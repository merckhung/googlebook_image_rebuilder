/* SPDX-License-Identifier: GPL-2.0 */
/* Run in the guest on a CPU without VEX AVX-IFMA. No libc is required.
 * Repeatedly read a vector spanning a resident page and a discarded page.
 * With an argument, make the second page inaccessible: expect SIGILL, with
 * the guest still responsive and no kernel page-fault warning or retry loop.
 */
static long call6(long n, long a, long b, long c, long d, long e, long f)
{
	register long r10 __asm__("r10") = d;
	register long r8 __asm__("r8") = e;
	register long r9 __asm__("r9") = f;
	long ret;
	__asm__ volatile("syscall" : "=a"(ret) : "a"(n), "D"(a), "S"(b), "d"(c),
			 "r"(r10), "r"(r8), "r"(r9) : "rcx", "r11", "memory");
	return ret;
}
static const char passed[] = "PASS: 256 cross-page AVX-IFMA reads\n";
static const unsigned long ones[4] = {1, 1, 1, 1};
__attribute__((used)) int test_main(unsigned long *stack)
{
	long address = call6(9, 0, 8192, 3, 0x22, -1, 0);
	unsigned long result[4], *operand;
	int i;
	if (address < 0)
		return 2;
	operand = (unsigned long *)(address + 4096 - 16);
	operand[0] = 2;
	operand[1] = 3;
	if (*stack > 1 && call6(10, address + 4096, 4096, 0, 0, 0, 0))
		return 3;
	for (i = 0; i < 256; i++) {
		if (call6(28, address + 4096, 4096, 4, 0, 0, 0))
			return 4;
		__asm__ volatile("vmovdqu (%1), %%ymm1\n\t"
				 "vpxor %%ymm0, %%ymm0, %%ymm0\n\t"
				 "%{vex%} vpmadd52luq (%2), %%ymm1, %%ymm0\n\t"
				 "vmovdqu %%ymm0, (%0)"
				 :: "r"(result), "r"(ones), "r"(operand)
				 : "ymm0", "ymm1", "memory");
		if (result[0] != 2 || result[1] != 3 || result[2] || result[3])
			return 5;
	}
	call6(1, 1, (long)passed, sizeof(passed) - 1, 0, 0, 0);
	return 0;
}
__asm__(".global _start\n_start:\nmov %rsp,%rdi\nand $-16,%rsp\ncall test_main\nmov %eax,%edi\nmov $60,%eax\nsyscall\nud2\n");
