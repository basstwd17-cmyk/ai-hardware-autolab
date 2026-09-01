;  pru_delay.asm - exact N-cycle runtime delay via the PRU hardware LOOP.
;  void pru_delay(uint32_t n);   delays EXACTLY (n & 0xffff) PRU NOP cycles
;  (at most 65,535 cycles, n * 5 ns @ 200 MHz)
;  with ZERO per-iteration overhead (no counter read, no spin) -> the runtime
;  window position becomes as precise as the (5 ns) timebase itself.
;
;  clpru ABI: first 32-bit arg in R14 ; leaf return via R3.w2.
	.global	pru_delay
	.sect	".text:pru_delay"
pru_delay:
	qbeq	pd_ret, r14, 0		; n == 0 -> nothing to do
	loop	pd_end, r14		; repeat the body (1 instr) r14 times
	nop				; 1-cycle loop body
pd_end:
pd_ret:
	jmp	r3.w2			; return to caller (leaf)
