#include "config.h"
#include "io.h"
#include "model.h"
#include <signal.h>

static volatile sig_atomic_t stop_requested;
static void request_stop(int signal_number)
{
    (void)signal_number;
    stop_requested = 1; // Только флаг; потоки завершаются в model_run.
}

int main(int argc, char **argv)
{
    struct sigaction action = {0};
    action.sa_handler = request_stop;
    if (sigemptyset(&action.sa_mask) < 0 || sigaction(SIGINT, &action, NULL) < 0) {
        io_error("обработчик SIGINT");
        return 1;
    }
    action.sa_handler = SIG_IGN;
    if (sigaction(SIGPIPE, &action, NULL) < 0) { io_error("обработчик SIGPIPE"); return 1; }
    Config config;
    int parsed = config_parse(&config, argc, argv);
    if (parsed) return parsed > 0 ? 0 : 2;
    Output output = {-1};
    Model *model = NULL;
    int code = 1;
    const char *reason = "error";
    if (output_open(&output, &config) < 0) goto cleanup;
    model = model_create(&config, output_event, &output);
    if (!model) { io_error("создание модели"); goto cleanup; }
    code = model_run(model, &stop_requested, &reason);
    if (code == 1) io_error("выполнение модели или запись события");
    char summary[512];
    if (model_summary(model, reason, summary, sizeof(summary)) < 0) {
        io_error("формирование итогов");
        code = 1;
    } else if (output_summary(&output, summary) < 0) code = 1;
cleanup:
    model_destroy(model);
    if (output_close(&output) < 0) code = 1;
    return code;
}
