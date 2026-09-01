# bbb_pulse_io PRU0 firmware build (clpru). Run on the BBB.
#   PRU_CGT = TI PRU codegen (apt: ti-pru-cgt-v2.3). SUPPORT = vendored TI subset
#   (pru-support/, offline — no support-package clone needed). Override either via env.
PRU_CGT ?= /usr/share/ti/cgt-pru
SUPPORT ?= ./pru-support

LINKER_COMMAND_FILE=./AM335x_PRU_intc_rscTbl.cmd
LIBS=--library=$(SUPPORT)/lib/rpmsg_lib.lib
INCLUDE=--include_path=$(SUPPORT)/include --include_path=$(SUPPORT)/include/am335x
STACK_SIZE=0x100
HEAP_SIZE=0x100
GEN_DIR=gen

CFLAGS=-v3 -O2 --display_error_number --endian=little --hardware_mac=on --obj_directory=$(GEN_DIR) --pp_directory=$(GEN_DIR) -ppd -ppa
LFLAGS=--reread_libs --warn_sections --stack_size=$(STACK_SIZE) --heap_size=$(HEAP_SIZE)

PROJ_NAME=pru0_pulse_io
TARGET=$(GEN_DIR)/$(PROJ_NAME).out
MAP=$(GEN_DIR)/$(PROJ_NAME).map
SOURCES=$(wildcard *.c)
ASMSRC=$(wildcard *.asm)
OBJECTS=$(patsubst %,$(GEN_DIR)/%,$(SOURCES:.c=.object)) $(patsubst %,$(GEN_DIR)/%,$(ASMSRC:.asm=.object))

all: $(TARGET)

$(TARGET): $(OBJECTS) $(LINKER_COMMAND_FILE)
	@echo 'Linking $@'
	$(PRU_CGT)/bin/clpru $(CFLAGS) -z -i$(PRU_CGT)/lib -i$(PRU_CGT)/include $(LFLAGS) -o $(TARGET) $(OBJECTS) -m$(MAP) $(LINKER_COMMAND_FILE) --library=libc.a $(LIBS)
	@echo 'Built: $(TARGET)'

$(GEN_DIR)/%.object: %.c
	@mkdir -p $(GEN_DIR)
	@echo 'Compiling $<'
	$(PRU_CGT)/bin/clpru --include_path=$(PRU_CGT)/include $(INCLUDE) $(CFLAGS) -fe $@ $<

$(GEN_DIR)/%.object: %.asm
	@mkdir -p $(GEN_DIR)
	@echo 'Assembling $<'
	$(PRU_CGT)/bin/clpru --include_path=$(PRU_CGT)/include $(CFLAGS) -fe $@ $<

.PHONY: all clean
clean:
	@rm -rf $(GEN_DIR)

-include $(OBJECTS:%.object=%.pp)
