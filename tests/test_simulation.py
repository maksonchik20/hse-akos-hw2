#!/usr/bin/env python3


import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import traceback


BINARY = Path(sys.argv[1]).resolve()
PROJECT = Path(__file__).resolve().parents[1]
PASSED = 0
FAILED = 0
SKIPPED = 0
FIELD = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)")
STATES = re.compile(r"\b(?:NORMAL|WILTING|WATERING|RECOVERING|DRIED|DEAD_OVERWATER)\b")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fields(text):
    return dict(FIELD.findall(text))


def require_clean_diagnostics(result):
    markers = ("ERROR: AddressSanitizer", "SUMMARY: AddressSanitizer", "LeakSanitizer", "ThreadSanitizer", "runtime error:")
    require(not any(marker in result.stderr for marker in markers),
            "Диагностика санитайзера: " + result.stderr[-2000:])


def summary(text):
    lines = [line for line in text.splitlines() if re.search(r"\bSUMMARY\b", line)]
    require(len(lines) == 1, "Нужна ровно одна итоговая строка SUMMARY: " + text[-1500:])
    nameToValue = fields(lines[0])
    for name in ("time", "created", "completed", "cancelled", "pending", "assigned",
                 "waterings", "conflicts", "overwater", "dried", "alive"):
        require(name in nameToValue, "В SUMMARY нет поля " + name)
        nameToValue[name] = int(nameToValue[name])
    require(nameToValue.get("invariants") == "ok", "Внутренняя проверка инвариантов не пройдена")
    return nameToValue


def parse_events(text):
    events = []
    for line in text.splitlines():
        if re.search(r"\bSUMMARY\b", line):
            continue
        nameToValue = fields(line)
        if "event" not in nameToValue:
            continue
        for name in ("t", "flower", "gardener", "request"):
            require(name in nameToValue, "В событии нет " + name + ": " + line)
            nameToValue[name] = int(nameToValue[name])
        nameToValue["raw"] = line
        events.append(nameToValue)
    return events


def matching(run, code):
    return [event for event in run["events"] if event["event"] == code]


def verify_trace(events, totals):
    requestIdToStatus = {}
    requestIdToFlower = {}
    requestIdToGardener = {}
    flowerIdToRequest = {}
    gardenerIdToRequest = {}
    flowerIdToState = {}
    flowerIdToDeathTime = {}
    requestIdToStartTime = {}
    requestIdToEndTime = {}
    gardenerIdToChoice = {}
    overwater_requests = set()
    codeToCount = {}
    last_time = -1
    last_request = 0
    for event in events:
        code, now = event["event"], event["t"]
        flower, gardener, request = event["flower"], event["gardener"], event["request"]
        require(now >= last_time, "Модельное время журнала уменьшилось")
        last_time = now
        codeToCount[code] = codeToCount.get(code, 0) + 1
        if flower in flowerIdToDeathTime:
            require(code in ("CANCEL", "DRIED", "OVERWATER"), "Активность после перехода в состояние гибели")
        if code == "STATE":
            states = STATES.findall(event["raw"])
            require(bool(states), "В STATE не указано состояние: " + event["raw"])
            state = states[-1]
            previous = flowerIdToState.get(flower, "NONE")
            valid_transitions = {("NONE", "NORMAL"), ("NORMAL", "WILTING"), ("NORMAL", "DRIED"),
                                 ("WILTING", "WATERING"), ("WILTING", "DRIED"),
                                 ("WILTING", "DEAD_OVERWATER"), ("WATERING", "RECOVERING"),
                                 ("RECOVERING", "NORMAL")}
            require((previous, state) in valid_transitions,
                    "Невозможный переход состояния: {} -> {}".format(previous, state))
            flowerIdToState[flower] = state
            if state in ("DRIED", "DEAD_OVERWATER"):
                flowerIdToDeathTime[flower] = now
        elif code == "REQUEST":
            require(request > last_request, "Номер новой заявки не увеличивается")
            require(flower not in flowerIdToRequest, "У цветка уже есть активная заявка")
            require(flower not in flowerIdToDeathTime, "Погибший цветок создал заявку")
            require(flowerIdToState.get(flower) == "WILTING", "Заявка создана не увядающим цветком")
            last_request = request
            requestIdToStatus[request] = "PENDING"
            requestIdToFlower[request] = flower
            flowerIdToRequest[flower] = request
        elif code == "CHOICE":
            require(requestIdToStatus.get(request) == "PENDING", "Выбрана не ожидающая заявка")
            require(requestIdToFlower[request] == flower, "Выбрана заявка другого цветка")
            require(gardener not in gardenerIdToRequest, "Занятый садовник выбирает новую заявку")
            require(request == min(r for r, status in requestIdToStatus.items() if status == "PENDING"),
                    "FIFO выбрала не самую старую заявку")
            gardenerIdToChoice[gardener] = request
        elif code == "CONFLICT":
            require(requestIdToStatus.get(request) == "ASSIGNED", "Конфликт без занятой заявки")
            require(gardenerIdToChoice.get(gardener) == request, "Конфликт без выбора этой заявки")
            winner = requestIdToGardener[request]
            require(gardener != winner, "Садовник конфликтует сам с собой")
            recorded_winner = re.search(r"\bwinner=(\d+)", event["raw"])
            require(recorded_winner and int(recorded_winner.group(1)) == winner,
                    "В конфликте указан неверный победитель")
        elif code == "ACCEPT":
            require(requestIdToStatus.get(request) == "PENDING", "Повторное принятие заявки")
            require(requestIdToFlower[request] == flower, "Изменился цветок заявки")
            require(gardener not in gardenerIdToRequest, "Садовник одновременно обслуживает два цветка")
            require(gardenerIdToChoice.get(gardener) == request, "Принятие без выбора этой заявки")
            require(request == min(r for r, status in requestIdToStatus.items() if status == "PENDING"),
                    "FIFO приняла заявку раньше более старой")
            requestIdToStatus[request] = "ASSIGNED"
            requestIdToGardener[request] = gardener
            gardenerIdToRequest[gardener] = request
        elif code == "WATER_START":
            require(requestIdToStatus.get(request) == "ASSIGNED", "Полив без принятой заявки")
            require(requestIdToGardener[request] == gardener, "Полив ведёт чужой садовник")
            require(flowerIdToState.get(flower) == "WATERING", "Полив не перевёл цветок в WATERING")
            require(request not in requestIdToStartTime, "Повторный полив по старой заявке")
            requestIdToStartTime[request] = now
        elif code == "WATER_END":
            require(request in requestIdToStartTime, "Окончание полива без начала")
            require(request not in requestIdToEndTime, "Двойное окончание полива")
            require(now > requestIdToStartTime[request], "Полив занял меньше одного такта")
            require(requestIdToGardener[request] == gardener, "Полив завершил другой садовник")
            requestIdToEndTime[request] = now
        elif code == "OVERWATER":
            require(requestIdToStatus.get(request) == "ASSIGNED", "Перелив без принятой заявки")
            require(request not in requestIdToStartTime, "Перелив ошибочно засчитан обычным поливом")
            require(request not in overwater_requests, "Повторное событие перелива для одной заявки")
            overwater_requests.add(request)
        elif code in ("COMPLETE", "CANCEL"):
            status = requestIdToStatus.get(request)
            require(status in ("PENDING", "ASSIGNED"), "Повторное завершение/снятие заявки")
            require(requestIdToFlower[request] == flower, "Снята заявка другого цветка")
            if code == "COMPLETE":
                require(status == "ASSIGNED", "Завершена непринятая заявка")
                require(request in requestIdToEndTime, "Заявка завершена до окончания полива")
            if status == "ASSIGNED":
                owner = requestIdToGardener[request]
                require(gardenerIdToRequest.get(owner) == request, "Потеряна занятость садовника")
                del gardenerIdToRequest[owner]
            require(flowerIdToRequest.get(flower) == request, "Потеряна активная заявка цветка")
            del flowerIdToRequest[flower]
            requestIdToStatus[request] = "COMPLETED" if code == "COMPLETE" else "CANCELLED"

    for flower in flowerIdToDeathTime:
        require(flower not in flowerIdToRequest, "У погибшего цветка осталась активная заявка")
    for request in overwater_requests:
        require(requestIdToStatus.get(request) == "CANCELLED", "Перелив не отменил заявку")
    for gardener, request in gardenerIdToRequest.items():
        require(request in requestIdToStartTime, "Назначенная заявка осталась без полива")
        require(flowerIdToState.get(requestIdToFlower[request]) == "WATERING", "Занятость не соответствует состоянию цветка")
    statusToCount = {status: list(requestIdToStatus.values()).count(status)
                     for status in ("PENDING", "ASSIGNED", "COMPLETED", "CANCELLED")}
    nameToExpected = {
        "created": len(requestIdToStatus),
        "completed": statusToCount["COMPLETED"],
        "cancelled": statusToCount["CANCELLED"],
        "pending": statusToCount["PENDING"],
        "assigned": statusToCount["ASSIGNED"],
        "waterings": len(requestIdToStartTime),
        "conflicts": codeToCount.get("CONFLICT", 0),
        "overwater": codeToCount.get("OVERWATER", 0),
        "dried": list(flowerIdToState.values()).count("DRIED"),
        "alive": len(flowerIdToState) - len(flowerIdToDeathTime),
    }
    for name, value in nameToExpected.items():
        require(totals[name] == value, "SUMMARY {}={}, по журналу {}".format(name, totals[name], value))
    require(totals["created"] == sum(statusToCount.values()), "Нарушен баланс заявок")
    require(totals["time"] >= max(last_time, 0), "Итоговое время раньше последнего события")


def load_run(directory, result, expected_code=0):
    require_clean_diagnostics(result)
    require(result.returncode == expected_code,
            "Код {} вместо {}; stderr: {}".format(result.returncode, expected_code, result.stderr[-2000:]))
    log = (directory / "events.log").read_text(encoding="utf-8")
    totals = summary(log)
    if result.stdout is not None:
        require(result.stdout == log, "Терминал и журнал различаются")
    events = parse_events(log)
    verify_trace(events, totals)
    return {"events": events, "summary": totals, "log": log,
            "stdout": result.stdout, "stderr": result.stderr}


def simulate(arguments=(), config=None):
    with tempfile.TemporaryDirectory(prefix="smart-flowers-test-") as temporary:
        directory = Path(temporary)
        command = [str(BINARY), "--log", "events.log"]
        if config:
            command += ["--config", str(PROJECT / "configs" / config)]
        command += list(arguments)
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=15)
        return load_run(directory, result)


def totals_are(run, **nameToExpected):
    for name, value in nameToExpected.items():
        require(run["summary"].get(name) == value,
                "Ожидалось {}={}, итог: {}".format(name, value, run["summary"]))


def test_ordinary():
    run = simulate(config="ordinary.conf")
    totals_are(run, reason="time_limit", cancelled=0, overwater=0, alive=1)
    require(run["summary"]["completed"] >= 1, "Не завершён ни один обычный полив")
    require(matching(run, "RECOVERY_END"), "Цветок не восстановился")


def test_conflict():
    run = simulate(config="conflict.conf")
    require(run["summary"]["waterings"] >= 4, "Садовники не разобрали очередь")
    require(len({event["gardener"] for event in matching(run, "ACCEPT")}) == 4,
            "Не все четыре садовника приняли заявки")
    require_overlap(run, 4)


def test_drought():
    run = simulate(config="drought.conf")
    require(run["summary"]["dried"] >= 2, "Длинный полив не вызвал засыхания очереди")
    require(run["summary"]["cancelled"] >= 2, "Заявки засохших цветов не отменены")


def test_overwater():
    run = simulate(config="overwater.conf")
    totals_are(run, reason="all_inactive", created=2, completed=1, cancelled=1,
               waterings=1, overwater=1, dried=0, alive=0, pending=0, assigned=0)
    require([event["request"] for event in matching(run, "OVERWATER")] == [2],
            "Перелив должен относиться к новой заявке после восстановления")


def test_protection_disabled():
    run = simulate(["--protection-interval", "0", "--duration", "20"], config="overwater.conf")
    totals_are(run, reason="time_limit", overwater=0, alive=1)
    require(run["summary"]["waterings"] >= 2, "Без защиты не было повторного полива")


def test_unlimited_all_inactive():
    run = simulate(["--unlimited", "true", "--duration", "1"], config="overwater.conf")
    totals_are(run, reason="all_inactive", overwater=1, alive=0)
    require(run["summary"]["time"] > 1, "Безлимитный режим остановился по duration")


def test_no_gardeners():
    run = simulate(["--flowers", "2", "--gardeners", "0", "--dry-rates", "1,2",
                    "--request-threshold", "2", "--death-threshold", "4", "--duration", "20"])
    totals_are(run, reason="all_inactive", dried=2, cancelled=2, created=2, alive=0)


def test_death_precedes_request():
    run = simulate(["--flowers", "1", "--dry-rates", "10", "--request-threshold", "2",
                    "--death-threshold", "4", "--duration", "20"])
    totals_are(run, reason="all_inactive", created=0, dried=1, alive=0)


def test_speed_rounding(speed, duration, minimum):
    run = simulate(["--request-threshold", "1", "--water-duration", str(duration),
                    "--speeds", str(speed), "--duration", "15"], config="ordinary.conf")
    requestIdToStart = {event["request"]: event["t"] for event in matching(run, "WATER_START")}
    endings = matching(run, "WATER_END")
    require(endings, "Нет завершённого полива для проверки скорости")
    for event in endings:
        require(event["t"] - requestIdToStart[event["request"]] >= minimum,
                "Полив завершён раньше ceil(water-duration / speed)")


def test_fifo_age():
    run = simulate(["--flowers", "8", "--gardeners", "1", "--dry-rates", "1",
                    "--request-threshold", "1", "--death-threshold", "1000", "--water-duration", "1",
                    "--recovery-duration", "1", "--protection-interval", "0", "--duration", "30"])
    accepted = matching(run, "ACCEPT")
    require(len(accepted) >= 9, "Очередь не дошла до повторного полива")
    require([event["request"] for event in accepted] == sorted(event["request"] for event in accepted),
            "Новая заявка обслужена раньше старой")
    require(len({event["flower"] for event in accepted[:8]}) == 8,
            "Повторный полив произошёл раньше обслуживания всей исходной очереди")


def require_overlap(run, count):
    active = set()
    maximum = 0
    for event in run["events"]:
        if event["event"] == "WATER_START":
            active.add(event["gardener"])
            maximum = max(maximum, len(active))
        elif event["event"] == "WATER_END":
            active.remove(event["gardener"])
    require(maximum >= count, "Ожидалось {} параллельных поливов, было {}".format(count, maximum))


def test_concurrent_runs():
    for delay in (0, 2, 10):
        for attempt in range(3):
            run = simulate(["--flowers", "12", "--gardeners", "4", "--dry-rates", "1",
                            "--request-threshold", "1", "--death-threshold", "1000", "--water-duration", "4",
                            "--recovery-duration", "1", "--protection-interval", "0", "--duration", "30",
                            "--delay-ms", str(delay)])
            totals_are(run, reason="time_limit", alive=12, overwater=0, cancelled=0)
            require(run["summary"]["completed"] >= (1 if delay == 2 else 12),
                    "Очередь зависла: delay={}, запуск={}".format(delay, attempt))
            require_overlap(run, 2 if delay == 2 else 4)


def test_two_speeds():
    run = simulate(["--flowers", "8", "--gardeners", "2", "--dry-rates", "1",
                    "--request-threshold", "1", "--death-threshold", "1000", "--water-duration", "6",
                    "--speeds", "1,3", "--recovery-duration", "1", "--protection-interval", "0",
                    "--duration", "30"])
    requestIdToStart = {event["request"]: event["t"] for event in matching(run, "WATER_START")}
    gardenerIdToMinimum = {1: 6, 2: 2}
    endings = matching(run, "WATER_END")
    require({event["gardener"] for event in endings} == {1, 2}, "Не оба садовника завершили работу")
    for event in endings:
        require(event["t"] - requestIdToStart[event["request"]] >= gardenerIdToMinimum[event["gardener"]],
                "Скорость садовника применена неправильно")
    require_overlap(run, 2)


def test_zero_time():
    run = simulate(["--duration", "0"])
    totals_are(run, reason="time_limit", time=0, created=0, alive=3)


def test_zero_flowers():
    run = simulate(["--flowers", "0", "--gardeners", "0", "--duration", "0"])
    totals_are(run, reason="all_inactive", time=0, created=0, alive=0)


def test_upper_bounds():
    run = simulate(["--flowers", "1000", "--gardeners", "1000", "--dry-rates", "1000000",
                    "--speeds", "1000000", "--request-threshold", "999999", "--death-threshold", "1000000",
                    "--water-duration", "1000000", "--recovery-duration", "1000000",
                    "--protection-interval", "1000000", "--delay-ms", "60000", "--duration", "0"])
    totals_are(run, reason="time_limit", time=0, alive=1000)


def test_config_precedence():
    with tempfile.TemporaryDirectory(prefix="smart-flowers-config-") as temporary:
        directory = Path(temporary)
        config = directory / "input.conf"
        config.write_text("# комментарий\nflowers=2\nflowers=3\ngardeners=0\n"
                          "dry-rates=2,3,4\nrequest-threshold=2\ndeath-threshold=6\n"
                          "duration=99\nunlimited=false\n", encoding="utf-8")
        override = ["--flowers", "1", "--dry-rates", "1", "--duration", "3"]
        for arguments in (["--config", str(config)] + override, override + ["--config", str(config)]):
            result = subprocess.run([str(BINARY)] + arguments, cwd=directory, capture_output=True, text=True, timeout=5)
            run = load_run(directory, result)
            totals_are(run, reason="time_limit", created=1, alive=1, pending=1)
        result = subprocess.run([str(BINARY), "--config", str(config), "--duration", "0"],
                                cwd=directory, capture_output=True, text=True, timeout=5)
        totals_are(load_run(directory, result), alive=3, time=0)
        config.write_text("flowers=invalid\nduration=invalid\n", encoding="utf-8")
        result = subprocess.run([str(BINARY), "--config", str(config), "--flowers", "0", "--duration", "0"],
                                cwd=directory, capture_output=True, text=True, timeout=5)
        totals_are(load_run(directory, result), alive=0, time=0)


def test_invalid(arguments, config_text=None):
    with tempfile.TemporaryDirectory(prefix="smart-flowers-invalid-") as temporary:
        directory = Path(temporary)
        command = [str(BINARY)] + list(arguments)
        if config_text is not None:
            config = directory / "bad.conf"
            config.write_text(config_text, encoding="utf-8")
            command += ["--config", str(config)]
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=5)
        require_clean_diagnostics(result)
        require(result.returncode in (1, 2),
                "Некорректный ввод принят либо программа аварийно завершилась (код {}): {}".format(
                    result.returncode, repr(arguments or config_text)))
        require(bool(result.stderr.strip()), "Для некорректного ввода нет пояснения в stderr")


def test_help():
    result = subprocess.run([str(BINARY), "--help"], capture_output=True, text=True, timeout=5)
    require(result.returncode == 0, "--help завершился с ошибкой")
    require(all(option in result.stdout for option in ("--config", "flowers", "unlimited", "FIFO")),
            "--help не описывает основные параметры")


def test_alias(kind):
    with tempfile.TemporaryDirectory(prefix="smart-flowers-alias-") as temporary:
        directory = Path(temporary)
        original = directory / "source"
        alias = directory / "alias"
        content = "flowers=1\nduration=0\n"
        original.write_text(content, encoding="utf-8")
        if kind == "hardlink":
            os.link(original, alias)
        elif kind == "symlink":
            alias.symlink_to(original)
        else:
            alias = original
        arguments = ["--config", str(original), "--log", str(alias)]
        result = subprocess.run([str(BINARY)] + arguments, cwd=directory, capture_output=True, text=True, timeout=5)
        require_clean_diagnostics(result)
        require(result.returncode in (1, 2), "Нет штатного отказа для совпадающих входных/выходных файлов")
        require(original.read_text(encoding="utf-8") == content, "Исходный файл повреждён при проверке alias")


def test_invalid_output_device():
    if not Path("/dev/full").exists():
        raise unittest_skip("/dev/full отсутствует на этой ОС")
    with tempfile.TemporaryDirectory(prefix="smart-flowers-full-") as temporary:
        result = subprocess.run([str(BINARY), "--duration", "1", "--log", "/dev/full"],
                                cwd=temporary, capture_output=True, text=True, timeout=5)
        require_clean_diagnostics(result)
        require(result.returncode in (1, 2), "Нет штатного отказа для устройства /dev/full")
        require(bool(result.stderr.strip()), "Ошибка выходного файла не объяснена")


def test_stdout_write_error():
    if not Path("/dev/full").exists():
        raise unittest_skip("/dev/full отсутствует на этой ОС")
    for duration in (0, 1):
        with tempfile.TemporaryDirectory(prefix="smart-flowers-stdout-full-") as temporary:
            with open("/dev/full", "wb") as output:
                result = subprocess.run([str(BINARY), "--duration", str(duration)], cwd=temporary,
                                        stdout=output, stderr=subprocess.PIPE, text=True, timeout=5)
            require_clean_diagnostics(result)
            require(result.returncode == 1, "Реальная ошибка write(stdout) потеряна либо вызвала аварийное завершение")
            require(bool(result.stderr.strip()), "Ошибка stdout не объяснена")
            statNameToValue = summary((Path(temporary) / "events.log").read_text(encoding="utf-8"))
            require(statNameToValue["reason"] == "error", "Ошибка начального вывода скрыта штатным завершением")
            require(statNameToValue["time"] == 0 and statNameToValue["alive"] == 3,
                    "При отказе начального вывода потеряно исходное состояние модели")


def test_runtime_stdout_failure():
    with tempfile.TemporaryDirectory(prefix="smart-flowers-pipe-") as temporary:
        directory = Path(temporary)
        arguments = [str(BINARY), "--flowers", "8", "--gardeners", "2", "--request-threshold", "1",
                     "--death-threshold", "1000", "--water-duration", "2", "--protection-interval", "0",
                     "--duration", "1000", "--delay-ms", "2"]
        process = subprocess.Popen(arguments, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            observed = b""
            deadline = time.monotonic() + 5
            while b"WATER_START" not in observed and time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 0.1)[0]:
                    part = os.read(process.stdout.fileno(), 65536)
                    if not part:
                        break
                    observed += part
            require(b"WATER_START" in observed, "Потоки не начали полив до закрытия stdout")
            process.stdout.close()
            process.stdout = None
            _, stderr = process.communicate(timeout=3)
            require(process.returncode == 1, "Ошибка вывода из рабочего потока потеряна")
            result = subprocess.CompletedProcess(arguments, process.returncode, None, stderr.decode())
            require_clean_diagnostics(result)
            require(result.stderr.strip(), "Ошибка stdout из рабочего потока не объяснена")
            totals_are({"summary": summary((directory / "events.log").read_text())}, reason="error")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_cancelled_notifications():
    run = simulate(["--flowers", "16", "--gardeners", "1", "--dry-rates", "1",
                    "--request-threshold", "1", "--death-threshold", "3", "--water-duration", "10",
                    "--recovery-duration", "1", "--protection-interval", "0", "--duration", "30"])
    require(run["summary"]["cancelled"] >= 15, "Не создана очередь отменённых заявок")
    require(run["summary"]["completed"] >= 2, "Старые уведомления мешают новым заявкам")


def test_interrupt(unlimited, delay, active=True, recovering=False):
    with tempfile.TemporaryDirectory(prefix="smart-flowers-signal-") as temporary:
        directory = Path(temporary)
        arguments = [str(BINARY), "--flowers", "20", "--gardeners", "1",
                     "--dry-rates", "1", "--request-threshold", "1", "--death-threshold", "1000000",
                     "--water-duration", "1000000", "--recovery-duration", "1", "--protection-interval", "0",
                     "--duration", "1000000", "--delay-ms", str(delay)]
        marker = b"WATER_START" if active else b"event=STATE"
        if recovering:
            arguments += ["--flowers", "1", "--gardeners", "2", "--water-duration", "1",
                          "--recovery-duration", "1000000"]
            marker = b"RECOVERY_START"
        if unlimited:
            arguments += ["--unlimited", "true"]
        process = subprocess.Popen(arguments, cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            ready = False
            while time.monotonic() < deadline:
                path = directory / "events.log"
                if path.exists() and marker in path.read_bytes():
                    ready = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            require(ready, "Программа не достигла события {} до SIGINT".format(marker))
            started = time.monotonic()
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=3)
            require(time.monotonic() - started < 2, "Потоки не проснулись вовремя после SIGINT")
            result = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
            run = load_run(directory, result, expected_code=130)
            totals_are(run, reason="interrupted")
            require(0 <= run["summary"]["time"] < 1000000, "Прерывание не остановило модель своевременно")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class unittest_skip(Exception):
    pass


def case(name, action):
    global PASSED, FAILED, SKIPPED
    try:
        action()
    except unittest_skip as error:
        SKIPPED += 1
        print("SKIP {}: {}".format(name, error), flush=True)
    except Exception as error:
        FAILED += 1
        print("FAIL {}: {}".format(name, error), flush=True)
        traceback.print_exc()
    else:
        PASSED += 1
        print("PASS " + name, flush=True)


def main():
    if not BINARY.is_file():
        print("Не найден исполняемый файл: {}. Сначала выполните make.".format(BINARY), file=sys.stderr)
        return 2
    checks = [
        ("обычный цикл и восстановление", test_ordinary),
        ("конкуренция четырёх садовников", test_conflict),
        ("засыхание в очереди", test_drought), ("перелив по новой заявке", test_overwater),
        ("повторный полив без защиты", test_protection_disabled),
        ("без лимита / все неактивны", test_unlimited_all_inactive),
        ("нет садовников / все неактивны", test_no_gardeners),
        ("засыхание раньше отправки заявки", test_death_precedes_request),
        ("скорость и округление вверх", lambda: test_speed_rounding(3, 7, 3)),
        ("минимум один такт полива", lambda: test_speed_rounding(1000000, 1, 1)),
        ("FIFO / возраст заявки", test_fifo_age), ("разные скорости садовников", test_two_speeds),
        ("повторные конкурентные запуски", test_concurrent_runs),
        ("уведомления после отмены очереди", test_cancelled_notifications),
        ("нулевой период наблюдения", test_zero_time), ("ноль цветов и садовников", test_zero_flowers),
        ("верхние границы параметров", test_upper_bounds),
        ("приоритет CLI / повторные ключи / итоговый размер списков", test_config_precedence),
        ("справка", test_help),
    ]
    for name, action in checks:
        case(name, action)
    invalid_cases = [
        ["--unknown", "1"], ["--flowers"], ["--flowers", "-1"], ["--flowers", "1001"],
        ["--flowers", "1.5"], ["--flowers", "18446744073709551616"], ["--gardeners", "1001"],
        ["--duration", "1000001"], ["--duration", "-1"], ["--delay-ms", "60001"],
        ["--water-duration", "0"], ["--recovery-duration", "0"], ["--request-threshold", "0"],
        ["--request-threshold", "30", "--death-threshold", "30"],
        ["--request-threshold", "31", "--death-threshold", "30"],
        ["--dry-rates", "0"], ["--dry-rates", "1000001"], ["--speeds", "0"],
        ["--flowers", "2", "--dry-rates", "1,2,3"], ["--gardeners", "3", "--speeds", "1,2"],
        ["--dry-rates", "1,"], ["--dry-rates", "1,,2"], ["--policy", "fifo"], ["--duration=0"], ["--unlimited"], ["--quiet"],
        ["--log", ""], ["--stats", ""], ["--config", "missing-file.conf"],
        ["--config", "a.conf", "--config", "b.conf"], ["--log", "missing-directory/log"],
        ["--unlimited", "1"], ["--dry-rates", "nan"],
    ]
    for arguments in invalid_cases:
        case("ошибка CLI " + repr(arguments), lambda arguments=arguments: test_invalid(arguments))
    for contents in ("unknown=1\n", "flowers\n", "flowers=\n", "flowers=no\n",
                     "unlimited=maybe\n", "dry_rates=1\n", "flowers=1\0\n", "duration=0\n" * 7000):
        case("ошибка конфигурации " + repr(contents[:50]), lambda contents=contents: test_invalid([], contents))
    for kind in ("same", "hardlink", "symlink"):
        case("защита файлов от alias: " + kind, lambda kind=kind: test_alias(kind))
    case("недопустимый выходной файл", test_invalid_output_device)
    case("ошибка write(stdout)", test_stdout_write_error)
    case("ошибка stdout из рабочего потока", test_runtime_stdout_failure)
    for unlimited in (False, True):
        for delay in (0, 50):
            case("SIGINT unlimited={} delay={}".format(unlimited, delay),
                 lambda unlimited=unlimited, delay=delay: test_interrupt(unlimited, delay))
    for unlimited in (False, True):
        case("SIGINT / пустая очередь и ожидание 60 секунд / unlimited={}".format(unlimited),
             lambda unlimited=unlimited: test_interrupt(unlimited, 60000, active=False))
        case("SIGINT / восстановление и спящие садовники / unlimited={}".format(unlimited),
             lambda unlimited=unlimited: test_interrupt(unlimited, 0, recovering=True))
    print("RESULT passed={} failed={} skipped={}".format(PASSED, FAILED, SKIPPED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
