#ifndef MODEL_H
#define MODEL_H
#include "config.h"
#include <signal.h>
#include <stddef.h>

typedef struct Model Model;
typedef int (*ModelEmit)(void *, uint64_t, const char *, size_t, size_t, uint64_t, const char *);
Model *model_create(const Config *config, ModelEmit emit, void *context);
int model_run(Model *model, const volatile sig_atomic_t *interrupted, const char **reason);
int model_check(const Model *model);
int model_summary(const Model *model, const char *reason, char *text, size_t size);
void model_destroy(Model *model);
#endif
