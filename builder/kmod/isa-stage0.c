/* SPDX-License-Identifier: GPL-2.0 */
/* Load ISA emulation before Android's Alder Lake-compiled first-stage init.
 * No libc, dynamic linker, or ISA extensions are needed by this loader. */
static long syscall4(long n, long a, long b, long c, long d)
{
    register long r10 __asm__("r10") = d;
    long result;
    __asm__ volatile("syscall" : "=a"(result)
                     : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10)
                     : "rcx", "r11", "memory");
    return result;
}
static void message(const char *s)
{
    unsigned long n = 0;
    while (s[n]) ++n;
    (void)syscall4(1, 1, (long)s, n, 0);
}
__attribute__((noreturn)) static void fail(const char *operation, long error)
{
    char digits[24];
    unsigned long n = error < 0 ? -error : error;
    int i = sizeof(digits);
    message("gbos-stage0: ");
    message(operation);
    message(" failed, errno=");
    do { digits[--i] = '0' + n % 10; n /= 10; } while (n);
    (void)syscall4(1, 2, (long)(digits + i), sizeof(digits) - i, 0);
    message("\n");
    for (;;) (void)syscall4(34, 0, 0, 0, 0); /* pause: keep PID 1 alive for diagnosis */
}
__attribute__((used, noreturn)) void stage0(unsigned long *initial_stack)
{
    char **argv = (char **)(initial_stack + 1);
    char **envp = argv + initial_stack[0] + 1;
    long fd = syscall4(257, -100, (long)"/lib/modules/gbos_isa_emu.ko", 0, 0);
    if (fd < 0) fail("open ISA module", fd);
    long result = syscall4(313, fd, (long)"", 0, 0);
    if (result < 0 && result != -17) fail("load ISA module", result);
    (void)syscall4(3, fd, 0, 0, 0);
    message("gbos-stage0: ISA compatibility loaded; starting Android init\n");
    result = syscall4(59, (long)"/init.gbos-real", (long)argv, (long)envp, 0);
    fail("exec Android init", result);
}
__asm__(".global _start\n.type _start,@function\n_start:\n"
        "mov %rsp,%rdi\nandq $-16,%rsp\ncall stage0\nud2\n");
