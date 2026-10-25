#include "model.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef enum { NORMAL, WILTING, WATERING, RECOVERING, DRIED, DEAD_OVERWATER } State;
typedef struct { Model *model; size_t index; pthread_t thread; } Worker;
typedef struct {
    Worker worker;
    State state;
    uint64_t dryness, rate, request, due, last_water, next_dry;
    size_t gardener;
} Flower;
typedef struct {
    Worker worker;
    uint64_t speed;
    size_t flower;
    sem_t *wake;
    int notified;
} Gardener;
struct Model {
    const Config *config;
    Flower *flowers;
    Gardener *gardeners;
    pthread_mutex_t mutex;
    struct timespec epoch;
    size_t flowers_started, gardeners_started;
    uint64_t time, created, completed, cancelled, waterings, conflicts;
    ModelEmit emit;
    void *context;
    const char *reason;
    int error, stopping;
};

static int fail(Model *m, int error)
{
    if (!m->error) m->error = error;
    m->stopping = 1;
    m->reason = "error";
    errno = m->error;
    return -1;
}

static void stop(Model *m, const char *reason)
{
    if (!m->stopping) { m->stopping = 1; m->reason = reason; }
}

static int refresh(Model *m)
{
    if (m->stopping) return 0;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return fail(m, errno);
    uint64_t elapsed = (uint64_t)(now.tv_sec - m->epoch.tv_sec) * UINT64_C(1000000000);
    if (now.tv_nsec >= m->epoch.tv_nsec) elapsed += (uint64_t)(now.tv_nsec - m->epoch.tv_nsec);
    else elapsed -= (uint64_t)(m->epoch.tv_nsec - now.tv_nsec);
    uint64_t tick = m->config->delay_ms ? m->config->delay_ms : 10;
    m->time = elapsed / (tick * UINT64_C(1000000));
    if (!m->config->unlimited && m->time >= m->config->duration) {
        m->time = m->config->duration;
        stop(m, "time_limit");
    }
    return 0;
}

static int event(Model *m, const char *code, size_t flower, size_t gardener,
                 uint64_t request, const char *detail)
{
    errno = 0;
    if (m->emit && m->emit(m->context, m->time, code, flower, gardener, request, detail))
        return fail(m, errno ? errno : EIO);
    return 0;
}

static int state_event(Model *m, size_t i)
{
    const char *names[] = {"NORMAL; Нормальное развитие", "WILTING; Цветок увядает",
        "WATERING; Цветок поливается", "RECOVERING; Цветок восстанавливается",
        "DRIED; Цветок засох", "DEAD_OVERWATER; Цветок погиб от перелива"};
    Flower *f = &m->flowers[i];
    return event(m, "STATE", i + 1, f->gardener, f->request, names[f->state]);
}

static int notify(Model *m)
{
    for (size_t i = 0; i < m->config->gardeners; ++i) {
        Gardener *g = &m->gardeners[i];
        if (g->wake && !g->notified) {
            if (sem_post(g->wake) < 0) return fail(m, errno);
            g->notified = 1;
        }
    }
    return 0;
}

static int dry_flower(Model *m, size_t i)
{
    Flower *f = &m->flowers[i];
    const Config *c = m->config;
    uint64_t steps = m->time - f->next_dry + 1;
    uint64_t remaining = c->death_threshold - f->dryness;
    f->dryness += steps > (remaining - 1) / f->rate ? remaining : steps * f->rate;
    f->next_dry = m->time + 1;
    if (f->dryness == c->death_threshold) {
        uint64_t request = f->request;
        f->state = DRIED;
        f->request = 0;
        if (request) ++m->cancelled;
        if (state_event(m, i) < 0) return -1;
        if (request && event(m, "CANCEL", i + 1, 0, request, "Заявка снята после засыхания") < 0) return -1;
        return event(m, "DRIED", i + 1, 0, request, "Цветок прекратил активность");
    }
    if (f->state == NORMAL && f->dryness >= c->request_threshold) {
        if (m->created == UINT64_MAX) return fail(m, EOVERFLOW);
        f->state = WILTING;
        f->request = ++m->created;
        if (state_event(m, i) < 0 ||
            event(m, "REQUEST", i + 1, 0, f->request, "Цветок отправил новую заявку") < 0) return -1;
        return notify(m);
    }
    return 0;
}

static int finish_watering(Model *m, size_t i)
{
    Flower *f = &m->flowers[i];
    if (m->config->recovery_duration > UINT64_MAX - m->time) return fail(m, EOVERFLOW);
    size_t gardener = f->gardener;
    uint64_t request = f->request;
    m->gardeners[gardener - 1].flower = 0;
    f->gardener = 0;
    f->request = 0;
    f->dryness = 0;
    f->state = RECOVERING;
    f->due = m->time + m->config->recovery_duration;
    ++m->completed;
    if (event(m, "WATER_END", i + 1, gardener, request, "Садовник закончил полив") < 0 ||
        event(m, "COMPLETE", i + 1, gardener, request, "Выполненная заявка снята") < 0 ||
        state_event(m, i) < 0) return -1;
    return event(m, "RECOVERY_START", i + 1, 0, 0, "Началось восстановление");
}

static int finish_recovery(Model *m, size_t i)
{
    Flower *f = &m->flowers[i];
    f->state = NORMAL;
    f->due = 0;
    f->next_dry = m->time + 1;
    if (state_event(m, i) < 0) return -1;
    return event(m, "RECOVERY_END", i + 1, 0, 0, "Цветок восстановился");
}

static int start_watering(Model *m, size_t i, size_t gardener)
{
    Flower *f = &m->flowers[i];
    uint64_t request = f->request;
    if (f->last_water && m->time - f->last_water < m->config->protection_interval) {
        f->state = DEAD_OVERWATER;
        f->request = 0;
        ++m->cancelled;
        if (event(m, "ACCEPT", i + 1, gardener, request, "Садовник принял новую заявку") < 0 ||
            event(m, "OVERWATER", i + 1, gardener, request, "Повторный полив внутри защиты: цветок погиб") < 0 ||
            state_event(m, i) < 0) return -1;
        return event(m, "CANCEL", i + 1, gardener, request, "Заявка снята после перелива");
    }
    uint64_t speed = m->gardeners[gardener - 1].speed;
    uint64_t duration = m->config->water_duration / speed + (m->config->water_duration % speed != 0);
    if (duration > UINT64_MAX - m->time) return fail(m, EOVERFLOW);
    f->state = WATERING;
    f->gardener = gardener;
    f->last_water = m->time;
    f->due = m->time + duration;
    m->gardeners[gardener - 1].flower = i + 1;
    ++m->waterings;
    if (event(m, "ACCEPT", i + 1, gardener, request, "Садовник единолично принял заявку") < 0 ||
        state_event(m, i) < 0) return -1;
    return event(m, "WATER_START", i + 1, gardener, request, "Начался полив; высыхание приостановлено");
}

static int sleep_ms(Model *m, long milliseconds)
{
    struct timespec delay = {0, milliseconds * 1000000};
    while (nanosleep(&delay, &delay) < 0) {
        if (errno == EINTR) continue;
        int error = errno;
        pthread_mutex_lock(&m->mutex);
        fail(m, error);
        pthread_mutex_unlock(&m->mutex);
        return -1;
    }
    return 0;
}

static int wait_until(Model *m, uint64_t deadline)
{
    for (;;) {
        pthread_mutex_lock(&m->mutex);
        refresh(m);
        int stopped = m->stopping, ready = m->time >= deadline;
        pthread_mutex_unlock(&m->mutex);
        if (stopped) return -1;
        if (ready) return 0;
        if (sleep_ms(m, 1) < 0) return -1;
    }
}

static void *flower_thread(void *argument)
{
    Worker *worker = argument;
    Model *m = worker->model;
    Flower *f = &m->flowers[worker->index];
    for (;;) {
        pthread_mutex_lock(&m->mutex);
        refresh(m);
        if (!m->stopping) {
            if ((f->state == NORMAL || f->state == WILTING) && m->time >= f->next_dry)
                dry_flower(m, worker->index);
            else if (f->state == RECOVERING && m->time >= f->due)
                finish_recovery(m, worker->index);
        }
        int done = m->stopping || f->state == DRIED || f->state == DEAD_OVERWATER;
        uint64_t next = f->state == RECOVERING ? f->due : m->time + 1;
        pthread_mutex_unlock(&m->mutex);
        if (done || wait_until(m, next) < 0) return NULL;
    }
}

static void *gardener_thread(void *argument)
{
    Worker *worker = argument;
    Model *m = worker->model;
    Gardener *g = &m->gardeners[worker->index];
    for (;;) {
        pthread_mutex_lock(&m->mutex);
        refresh(m);
        size_t choice = 0;
        if (!m->stopping) {
            for (size_t i = 0; i < m->config->flowers; ++i) {
                Flower *f = &m->flowers[i];
                if (f->state == WILTING && (!choice || f->request < m->flowers[choice - 1].request))
                    choice = i + 1;
            }
        }
        uint64_t request = choice ? m->flowers[choice - 1].request : 0;
        if (choice) event(m, "CHOICE", choice, worker->index + 1, request, "Выбрана самая старая заявка FIFO");
        int stopped = m->stopping;
        pthread_mutex_unlock(&m->mutex);
        if (stopped) return NULL;
        if (!choice) {
            int result;
            do { result = sem_wait(g->wake); } while (result < 0 && errno == EINTR);
            int error = errno;
            pthread_mutex_lock(&m->mutex);
            if (result < 0) fail(m, error);
            else g->notified = 0;
            pthread_mutex_unlock(&m->mutex);
            if (result < 0) return NULL;
            continue;
        }
        if (sleep_ms(m, 1) < 0) return NULL; // Даём другим садовникам тоже откликнуться.
        pthread_mutex_lock(&m->mutex);
        refresh(m);
        Flower *f = &m->flowers[choice - 1];
        uint64_t due = 0;
        if (!m->stopping && f->request == request) {
            if (f->state == WILTING) {
                start_watering(m, choice - 1, worker->index + 1);
                if (g->flower) due = f->due;
            } else if (f->state == WATERING) {
                char detail[96];
                snprintf(detail, sizeof(detail), "winner=%zu loser=%zu; Заявку уже забрал другой садовник", f->gardener, worker->index + 1);
                ++m->conflicts;
                event(m, "CONFLICT", choice, worker->index + 1, request, detail);
            }
        }
        stopped = m->stopping;
        pthread_mutex_unlock(&m->mutex);
        if (stopped) return NULL;
        if (due) {
            if (wait_until(m, due) < 0) return NULL;
            pthread_mutex_lock(&m->mutex);
            refresh(m);
            if (!m->stopping) finish_watering(m, choice - 1);
            pthread_mutex_unlock(&m->mutex);
        }
    }
}

Model *model_create(const Config *config, ModelEmit emit, void *context)
{
    Model *m = calloc(1, sizeof(*m));
    if (!m) return NULL;
    int error = pthread_mutex_init(&m->mutex, NULL);
    if (error) { free(m); errno = error; return NULL; }
    m->config = config;
    m->emit = emit;
    m->context = context;
    m->flowers = calloc(config->flowers ? (size_t)config->flowers : 1, sizeof(*m->flowers));
    m->gardeners = calloc(config->gardeners ? (size_t)config->gardeners : 1, sizeof(*m->gardeners));
    if (!m->flowers || !m->gardeners) { model_destroy(m); errno = ENOMEM; return NULL; }
    for (size_t i = 0; i < config->flowers; ++i) {
        m->flowers[i].worker.model = m;
        m->flowers[i].worker.index = i;
        m->flowers[i].rate = config->dry_rates[i];
        m->flowers[i].next_dry = 1;
    }
    for (size_t i = 0; i < config->gardeners; ++i) {
        m->gardeners[i].worker.model = m;
        m->gardeners[i].worker.index = i;
        m->gardeners[i].speed = config->speeds[i];
    }
    for (size_t i = 0; i < config->flowers; ++i)
        if (state_event(m, i) < 0) break;
    return m;
}

static sem_t *open_semaphore(size_t index)
{
    char name[64];
    snprintf(name, sizeof(name), "/sf-%ld-%zu", (long)getpid(), index);
    sem_t *semaphore = sem_open(name, O_CREAT | O_EXCL, 0600, 0);
    if (semaphore == SEM_FAILED) return NULL;
    if (sem_unlink(name) < 0) {
        int error = errno;
        sem_close(semaphore);
        errno = error;
        return NULL;
    }
    return semaphore;
}

int model_run(Model *m, const volatile sig_atomic_t *interrupted, const char **reason)
{
    sigset_t blocked, previous;
    int mask_changed = 0;
    if (m->error) goto finished;
    if (*interrupted) { stop(m, "interrupted"); goto finished; }
    if (!m->config->flowers) { stop(m, "all_inactive"); goto finished; }
    if (!m->config->unlimited && !m->config->duration) { stop(m, "time_limit"); goto finished; }
    if (sigemptyset(&blocked) < 0 || sigaddset(&blocked, SIGINT) < 0) { fail(m, errno); goto finished; }
    int error = pthread_sigmask(SIG_BLOCK, &blocked, &previous);
    if (error) { fail(m, error); goto finished; }
    mask_changed = 1;
    for (size_t i = 0; i < m->config->gardeners; ++i) {
        m->gardeners[i].wake = open_semaphore(i + 1);
        if (!m->gardeners[i].wake) { fail(m, errno); goto launched; }
    }
    pthread_mutex_lock(&m->mutex);
    for (size_t i = 0; i < m->config->gardeners; ++i) {
        Worker *w = &m->gardeners[i].worker;
        error = pthread_create(&w->thread, NULL, gardener_thread, w);
        if (error) { fail(m, error); goto release; }
        ++m->gardeners_started;
    }
    for (size_t i = 0; i < m->config->flowers; ++i) {
        Worker *w = &m->flowers[i].worker;
        error = pthread_create(&w->thread, NULL, flower_thread, w);
        if (error) { fail(m, error); goto release; }
        ++m->flowers_started;
    }
    if (clock_gettime(CLOCK_MONOTONIC, &m->epoch) < 0) fail(m, errno);
release:
    pthread_mutex_unlock(&m->mutex);
launched:
    if (mask_changed) {
        error = pthread_sigmask(SIG_SETMASK, &previous, NULL);
        if (error) {
            pthread_mutex_lock(&m->mutex);
            fail(m, error);
            pthread_mutex_unlock(&m->mutex);
        }
    }
    for (;;) {
        pthread_mutex_lock(&m->mutex);
        if (*interrupted) stop(m, "interrupted");
        refresh(m);
        size_t alive = 0;
        for (size_t i = 0; i < m->config->flowers; ++i)
            alive += m->flowers[i].state != DRIED && m->flowers[i].state != DEAD_OVERWATER;
        if (!alive) stop(m, "all_inactive");
        int stopped = m->stopping;
        if (stopped) notify(m);
        pthread_mutex_unlock(&m->mutex);
        if (stopped) break;
        if (sleep_ms(m, 1) < 0) continue;
    }
    for (size_t i = 0; i < m->flowers_started; ++i) {
        error = pthread_join(m->flowers[i].worker.thread, NULL);
        if (error) { pthread_mutex_lock(&m->mutex); fail(m, error); pthread_mutex_unlock(&m->mutex); }
    }
    for (size_t i = 0; i < m->gardeners_started; ++i) {
        error = pthread_join(m->gardeners[i].worker.thread, NULL);
        if (error) { pthread_mutex_lock(&m->mutex); fail(m, error); pthread_mutex_unlock(&m->mutex); }
    }
finished:
    if (model_check(m) < 0) fail(m, errno);
    *reason = m->reason;
    if (m->error) { errno = m->error; return 1; }
    return strcmp(m->reason, "interrupted") == 0 ? 130 : 0;
}

int model_check(const Model *m)
{
    size_t pending = 0, assigned = 0;
    for (size_t i = 0; i < m->config->flowers; ++i) {
        const Flower *f = &m->flowers[i];
        int active = f->state == WILTING || f->state == WATERING;
        int timed = f->state == WATERING || f->state == RECOVERING;
        if (f->state < NORMAL || f->state > DEAD_OVERWATER || f->dryness > m->config->death_threshold ||
            active != (f->request != 0) || (f->state == WATERING) != (f->gardener != 0) ||
            f->request > m->created || f->gardener > m->config->gardeners || timed != (f->due != 0)) goto invalid;
        if ((f->state == NORMAL && f->dryness >= m->config->request_threshold) ||
            (active && (f->dryness < m->config->request_threshold || f->dryness >= m->config->death_threshold)) ||
            (f->state == RECOVERING && f->dryness != 0) ||
            (f->state == DRIED && f->dryness != m->config->death_threshold)) goto invalid;
        if (f->gardener && m->gardeners[f->gardener - 1].flower != i + 1) goto invalid;
        for (size_t j = 0; j < i; ++j)
            if (f->request && f->request == m->flowers[j].request) goto invalid;
        pending += f->state == WILTING;
        assigned += f->state == WATERING;
    }
    for (size_t i = 0; i < m->config->gardeners; ++i) {
        size_t flower = m->gardeners[i].flower;
        if (flower > m->config->flowers || (flower && m->flowers[flower - 1].gardener != i + 1)) goto invalid;
    }
    if (m->completed > m->created || m->cancelled > m->created - m->completed ||
        m->created - m->completed - m->cancelled != pending + assigned ||
        m->waterings < m->completed || m->waterings - m->completed != assigned) goto invalid;
    return 0;
invalid:
    errno = EINVAL;
    return -1;
}

int model_summary(const Model *m, const char *reason, char *text, size_t size)
{
    size_t pending = 0, assigned = 0, dried = 0, overwater = 0;
    for (size_t i = 0; i < m->config->flowers; ++i) {
        State state = m->flowers[i].state;
        pending += state == WILTING;
        assigned += state == WATERING;
        dried += state == DRIED;
        overwater += state == DEAD_OVERWATER;
    }
    int n = snprintf(text, size,
        "SUMMARY reason=%s time=%" PRIu64 " created=%" PRIu64 " completed=%" PRIu64
        " cancelled=%" PRIu64 " pending=%zu assigned=%zu waterings=%" PRIu64
        " conflicts=%" PRIu64 " overwater=%zu dried=%zu alive=%" PRIu64 " invariants=%s\n",
        reason, m->time, m->created, m->completed, m->cancelled, pending, assigned,
        m->waterings, m->conflicts, overwater, dried, m->config->flowers - dried - overwater,
        model_check(m) == 0 ? "ok" : "FAILED");
    if (n < 0 || (size_t)n >= size) { errno = ENOSPC; return -1; }
    return 0;
}

void model_destroy(Model *m)
{
    if (!m) return;
    if (m->gardeners)
        for (size_t i = 0; i < m->config->gardeners; ++i)
            if (m->gardeners[i].wake) sem_close(m->gardeners[i].wake);
    pthread_mutex_destroy(&m->mutex);
    free(m->flowers);
    free(m->gardeners);
    free(m);
}
