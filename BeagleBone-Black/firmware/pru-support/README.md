# pru-support — vendored TI PRU support subset

These are unmodified files from the **TI PRU Software Support Package**
(<https://git.ti.com/cgit/pru-software-support-package>), vendored here so the
firmware builds **offline** without cloning the full 28 MB package on the board.

- `include/` — common PRU headers (`pru_rpmsg.h`, `rsc_types.h`, …)
- `include/am335x/` — AM335x device headers (`pru_cfg.h`, `pru_ecap.h`, `pru_iep.h`, …)
- `lib/rpmsg_lib.lib` — prebuilt PRU RPMsg library

Also vendored one level up in `firmware/` (stock, from
`examples/am335x/PRU_RPMsg_Echo_Interrupt0/`): `resource_table.h`, `intc_map_0.h`,
`AM335x_PRU_intc_rscTbl.cmd`.

License: BSD-3-Clause (© Texas Instruments). See the per-file headers.
Only the `am335x` device set is included; other SoCs were dropped to stay minimal.
