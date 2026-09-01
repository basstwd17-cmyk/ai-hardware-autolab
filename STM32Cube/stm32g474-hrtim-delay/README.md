# stm32g474-hrtim-delay

STM32CubeIDE project for the ST B-G474E-DPOW1 discovery board.

## Hardware

- Board: B-G474E-DPOW1
- MCU: STM32G474RETx
- Main firmware entry point: `Core/Src/main.c`
- Linker scripts: `STM32G474RETX_FLASH.ld`, `STM32G474RETX_RAM.ld`

## Current Firmware

The current firmware configures HRTIM Timer D to generate short pulse outputs:

- PB14 / TD1: approximately 100 ns high pulse every 1 us
- PB15 / TD2: approximately 100 ns high pulse delayed from TD1
- SYSCLK: 170 MHz
- HRTIM prescaler: MUL32

## Import and Build

1. Open STM32CubeIDE.
2. Select `File > Import... > Existing Projects into Workspace`.
3. Select this repository folder: `C:\KIST\stm32g474-hrtim-delay`.
4. Import the `stm32g474-hrtim-delay` project.
5. Run `Project > Clean...`.
6. Build the `Debug` configuration.
7. Confirm that `Debug/stm32g474-hrtim-delay.elf` is generated.

## Debug

1. Connect the B-G474E-DPOW1 board by ST-LINK USB.
2. Open `stm32g474-hrtim-delay.launch`.
3. Start Debug or Run from STM32CubeIDE.
4. Check TD1/TD2 pulse outputs with an oscilloscope.

## Git Workflow

Before starting work:

```powershell
git pull
```

After editing C source or CubeIDE configuration:

```powershell
git status
git add .
git commit -m "Describe the firmware change"
git push
```

Do not commit generated `Debug/` or `Release/` build outputs.
