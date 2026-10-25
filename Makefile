CC ?= cc
CPPFLAGS ?= -D_POSIX_C_SOURCE=200809L
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Wpedantic
LDFLAGS ?=
ASAN_OPTIONS ?= detect_leaks=1:halt_on_error=1
UBSAN_OPTIONS ?= halt_on_error=1
BUILD ?= build
SOURCES = src/main.c src/config.c src/io.c src/model.c
OBJECTS = $(SOURCES:src/%.c=$(BUILD)/%.o)
TARGET = $(BUILD)/smart-flowers

.PHONY: all test sanitize test-sanitize clean
all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CC) $(CFLAGS) -pthread $(OBJECTS) $(LDFLAGS) -o $@

$(BUILD)/%.o: src/%.c
	@mkdir -p $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -pthread -MMD -MP -c $< -o $@

-include $(OBJECTS:.o=.d)

test: all
	./tests/run_tests.sh ./$(TARGET)

sanitize:
	$(MAKE) BUILD=build-sanitize CFLAGS='-std=c11 -O1 -g -Wall -Wextra -Wpedantic -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' all

test-sanitize: sanitize
	ASAN_OPTIONS='$(ASAN_OPTIONS)' UBSAN_OPTIONS='$(UBSAN_OPTIONS)' ./tests/run_tests.sh ./build-sanitize/smart-flowers

clean:
	rm -rf build build-sanitize
