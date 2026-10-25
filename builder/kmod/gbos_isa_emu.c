// SPDX-License-Identifier: GPL-2.0
/*
 * gbos_isa_emu.ko: Loadable GKI kernel module for x86-64 instruction emulation
 * (AVX-VNNI, AVX-VNNI-INT8, AVX-IFMA, GFNI, SHA-NI, 256-bit VAES, VPCLMULQDQ).
 *
 * Hooks the #UD (X86_TRAP_UD) path via register_die_notifier() (DIE_TRAP).
 * On Intel 12th Gen Alder Lake (ASUS Chromebook Plus CX34 CX3402, i3-1215U),
 * AVX-VNNI, GFNI, SHA-NI, VAES, and VPCLMULQDQ run natively in hardware with
 * zero traps, while AVX-IFMA instructions in userspace are safely emulated on
 * demand. On pre-Alder-Lake or Ice Lake Xeon KVM hosts lacking VEX AVX-VNNI,
 * VEX AVX-VNNI instructions are transparently emulated in-place.
 */
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kdebug.h>
#include <linux/module.h>
#include <linux/notifier.h>
#include <linux/ptrace.h>
#include <linux/sched.h>
#include <linux/types.h>
#include <linux/uaccess.h>
#include "isa-user-memory.h"
#include <asm/traps.h>

static inline u32 ld32(const u8 *p, int i)
{
	p += 4 * i;
	return (u32)p[0] | (u32)p[1] << 8 | (u32)p[2] << 16 | (u32)p[3] << 24;
}

static inline void st32(u8 *p, int i, u32 v)
{
	p += 4 * i;
	p[0] = v;
	p[1] = v >> 8;
	p[2] = v >> 16;
	p[3] = v >> 24;
}

static inline u64 ld64(const u8 *p, int i)
{
	return (u64)ld32(p, 2 * i) | (u64)ld32(p, 2 * i + 1) << 32;
}

static inline void st64(u8 *p, int i, u64 v)
{
	st32(p, 2 * i, (u32)v);
	st32(p, 2 * i + 1, (u32)(v >> 32));
}

/* ---- GFNI ---------------------------------------------------------------- */

static u8 gf_mul(u8 a, u8 b)
{
	u8 r = 0;

	while (b) {
		if (b & 1)
			r ^= a;
		a = (a << 1) ^ ((a & 0x80) ? 0x1b : 0);
		b >>= 1;
	}
	return r;
}

static u8 gf_inv(u8 x)
{
	u8 r = 1, p = x;
	int e = 254;

	if (!x)
		return 0;
	while (e) {
		if (e & 1)
			r = gf_mul(r, p);
		p = gf_mul(p, p);
		e >>= 1;
	}
	return r;
}

static u8 affine_byte(const u8 *matrix_qword, u8 x, u8 imm)
{
	u8 r = 0;
	int i;

	for (i = 0; i < 8; i++)
		r |= (parity8(matrix_qword[7 - i] & x) ^ ((imm >> i) & 1)) << i;
	return r;
}

static void emu_gf2p8affineqb(u8 *dst, const u8 *x, const u8 *m, u8 imm, int len)
{
	u8 out[32];
	int i;

	for (i = 0; i < len; i++)
		out[i] = affine_byte(m + (i & ~7), x[i], imm);
	for (i = 0; i < len; i++)
		dst[i] = out[i];
}

static void emu_gf2p8affineinvqb(u8 *dst, const u8 *x, const u8 *m, u8 imm, int len)
{
	u8 out[32];
	int i;

	for (i = 0; i < len; i++)
		out[i] = affine_byte(m + (i & ~7), gf_inv(x[i]), imm);
	for (i = 0; i < len; i++)
		dst[i] = out[i];
}

static void emu_gf2p8mulb(u8 *dst, const u8 *a, const u8 *b, int len)
{
	u8 out[32];
	int i;

	for (i = 0; i < len; i++)
		out[i] = gf_mul(a[i], b[i]);
	for (i = 0; i < len; i++)
		dst[i] = out[i];
}

/* ---- AVX-VNNI / AVX-VNNI-INT8 -------------------------------------------- */

static u32 sat32(s64 v)
{
	if (v > 0x7fffffffLL)
		return 0x7fffffff;
	if (v < -0x80000000LL)
		return 0x80000000u;
	return (u32)v;
}

static void emu_vpdpbusd(u8 *acc, const u8 *a, const u8 *b, int len, int saturate)
{
	int i, k;

	for (i = 0; i < len / 4; i++) {
		s64 sum = 0;

		for (k = 0; k < 4; k++)
			sum += (s64)a[4 * i + k] * (s8)b[4 * i + k];
		if (saturate)
			st32(acc, i, sat32((s64)(s32)ld32(acc, i) + sum));
		else
			st32(acc, i, ld32(acc, i) + (u32)sum);
	}
}

static void emu_vpdpwssd(u8 *acc, const u8 *a, const u8 *b, int len, int saturate)
{
	int i, k;

	for (i = 0; i < len / 4; i++) {
		s64 sum = 0;

		for (k = 0; k < 2; k++) {
			s16 x = (s16)(a[4 * i + 2 * k] | a[4 * i + 2 * k + 1] << 8);
			s16 y = (s16)(b[4 * i + 2 * k] | b[4 * i + 2 * k + 1] << 8);

			sum += (s64)x * y;
		}
		if (saturate)
			st32(acc, i, sat32((s64)(s32)ld32(acc, i) + sum));
		else
			st32(acc, i, ld32(acc, i) + (u32)sum);
	}
}

static void emu_vpdp8(u8 *acc, const u8 *a, const u8 *b, int len, int saturate,
		      int a_signed, int b_signed)
{
	int i, k;

	for (i = 0; i < len / 4; i++) {
		s64 sum = 0;

		for (k = 0; k < 4; k++) {
			s64 x = a_signed ? (s64)(s8)a[4 * i + k] : (s64)a[4 * i + k];
			s64 y = b_signed ? (s64)(s8)b[4 * i + k] : (s64)b[4 * i + k];

			sum += x * y;
		}
		if (saturate)
			st32(acc, i, sat32((s64)(s32)ld32(acc, i) + sum));
		else
			st32(acc, i, ld32(acc, i) + (u32)sum);
	}
}

/* ---- AVX-IFMA ------------------------------------------------------------ */

static void emu_vpmadd52(u8 *acc, const u8 *a, const u8 *b, int len, int high)
{
	const u64 m52 = (1ULL << 52) - 1;
	int i;

	for (i = 0; i < len / 8; i++) {
		unsigned __int128 p = (unsigned __int128)(ld64(a, i) & m52) *
				      (ld64(b, i) & m52);
		u64 r = high ? (u64)(p >> 52) & m52 : (u64)p & m52;

		st64(acc, i, ld64(acc, i) + r);
	}
}

/* ---- VAES & VPCLMULQDQ --------------------------------------------------- */

enum { EMU_AESENC, EMU_AESENCLAST, EMU_AESDEC, EMU_AESDECLAST };
static u8 aes_sbox[256], aes_isbox[256];

static void aes_init_tables(void)
{
	int x;

	for (x = 0; x < 256; x++) {
		u8 b = gf_inv((u8)x);
		u8 s = b ^ (u8)((b << 1) | (b >> 7)) ^ (u8)((b << 2) | (b >> 6)) ^
		       (u8)((b << 3) | (b >> 5)) ^ (u8)((b << 4) | (b >> 4)) ^ 0x63;

		aes_sbox[x] = s;
		aes_isbox[s] = (u8)x;
	}
}

static void aes_lane(u8 *out, const u8 *in, const u8 *rk, int op)
{
	u8 s[16], t[16];
	int inv = op >= EMU_AESDEC;
	int last = op == EMU_AESENCLAST || op == EMU_AESDECLAST;
	int r, c, i;

	for (i = 0; i < 16; i++)
		s[i] = in[i];
	for (c = 0; c < 4; c++)
		for (r = 0; r < 4; r++)
			t[r + 4 * c] = s[r + 4 * ((c + (inv ? 4 - r : r)) & 3)];
	for (i = 0; i < 16; i++)
		t[i] = inv ? aes_isbox[t[i]] : aes_sbox[t[i]];
	if (!last) {
		for (c = 0; c < 4; c++) {
			u8 *col = t + 4 * c;
			u8 a0 = col[0], a1 = col[1], a2 = col[2], a3 = col[3];

			if (!inv) {
				col[0] = gf_mul(a0, 2) ^ gf_mul(a1, 3) ^ a2 ^ a3;
				col[1] = a0 ^ gf_mul(a1, 2) ^ gf_mul(a2, 3) ^ a3;
				col[2] = a0 ^ a1 ^ gf_mul(a2, 2) ^ gf_mul(a3, 3);
				col[3] = gf_mul(a0, 3) ^ a1 ^ a2 ^ gf_mul(a3, 2);
			} else {
				col[0] = gf_mul(a0, 14) ^ gf_mul(a1, 11) ^ gf_mul(a2, 13) ^ gf_mul(a3, 9);
				col[1] = gf_mul(a0, 9) ^ gf_mul(a1, 14) ^ gf_mul(a2, 11) ^ gf_mul(a3, 13);
				col[2] = gf_mul(a0, 13) ^ gf_mul(a1, 9) ^ gf_mul(a2, 14) ^ gf_mul(a3, 11);
				col[3] = gf_mul(a0, 11) ^ gf_mul(a1, 13) ^ gf_mul(a2, 9) ^ gf_mul(a3, 14);
			}
		}
	}
	for (i = 0; i < 16; i++)
		out[i] = t[i] ^ rk[i];
}

static void emu_vaes(u8 *dst, const u8 *state, const u8 *rk, int len, int op)
{
	u8 out[32];
	int l;

	for (l = 0; l < len / 16; l++)
		aes_lane(out + 16 * l, state + 16 * l, rk + 16 * l, op);
	for (l = 0; l < len; l++)
		dst[l] = out[l];
}

static void emu_vpclmulqdq(u8 *dst, const u8 *a, const u8 *b, u8 imm, int len)
{
	u8 out[32];
	int l, i;

	for (l = 0; l < len / 16; l++) {
		u64 x = ld64(a + 16 * l, imm & 1 ? 1 : 0);
		u64 y = ld64(b + 16 * l, imm & 0x10 ? 1 : 0);
		u64 lo = 0, hi = 0;

		for (i = 0; i < 64; i++) {
			if ((y >> i) & 1) {
				lo ^= x << i;
				if (i)
					hi ^= x >> (64 - i);
			}
		}
		st64(out + 16 * l, 0, lo);
		st64(out + 16 * l, 1, hi);
	}
	for (i = 0; i < len; i++)
		dst[i] = out[i];
}

/* ---- SHA-NI -------------------------------------------------------------- */

static u32 sha1_f(int fn, u32 b, u32 c, u32 d)
{
	switch (fn) {
	case 0: return (b & c) ^ (~b & d);
	case 2: return (b & c) ^ (b & d) ^ (c & d);
	default: return b ^ c ^ d;
	}
}

static void emu_sha1rnds4(u8 *dst, const u8 *src, u8 imm)
{
	static const u32 K[4] = { 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6 };
	u32 a = ld32(dst, 3), b = ld32(dst, 2), c = ld32(dst, 1), d = ld32(dst, 0), e = 0;
	u32 w[4] = { ld32(src, 3), ld32(src, 2), ld32(src, 1), ld32(src, 0) };
	int fn = imm & 3, i;

	for (i = 0; i < 4; i++) {
		u32 t = sha1_f(fn, b, c, d) + rol32(a, 5) + w[i] + e + K[fn];

		e = d; d = c; c = rol32(b, 30); b = a; a = t;
	}
	st32(dst, 3, a); st32(dst, 2, b); st32(dst, 1, c); st32(dst, 0, d);
}

static void emu_sha1nexte(u8 *dst, const u8 *src)
{
	u32 t = rol32(ld32(dst, 3), 30);
	int i;

	for (i = 0; i < 3; i++)
		st32(dst, i, ld32(src, i));
	st32(dst, 3, ld32(src, 3) + t);
}

static void emu_sha1msg1(u8 *dst, const u8 *src)
{
	u32 w0 = ld32(dst, 3), w1 = ld32(dst, 2), w2 = ld32(dst, 1), w3 = ld32(dst, 0);
	u32 w4 = ld32(src, 3), w5 = ld32(src, 2);

	st32(dst, 3, w2 ^ w0); st32(dst, 2, w3 ^ w1);
	st32(dst, 1, w4 ^ w2); st32(dst, 0, w5 ^ w3);
}

static void emu_sha1msg2(u8 *dst, const u8 *src)
{
	u32 w13 = ld32(src, 2), w14 = ld32(src, 1), w15 = ld32(src, 0);
	u32 w16 = rol32(ld32(dst, 3) ^ w13, 1);
	u32 w17 = rol32(ld32(dst, 2) ^ w14, 1);
	u32 w18 = rol32(ld32(dst, 1) ^ w15, 1);
	u32 w19 = rol32(ld32(dst, 0) ^ w16, 1);

	st32(dst, 3, w16); st32(dst, 2, w17); st32(dst, 1, w18); st32(dst, 0, w19);
}

static void emu_sha256rnds2(u8 *dst, const u8 *src, const u8 *xmm0)
{
	u32 a = ld32(src, 3), b = ld32(src, 2), c = ld32(dst, 3), d = ld32(dst, 2);
	u32 e = ld32(src, 1), f = ld32(src, 0), g = ld32(dst, 1), h = ld32(dst, 0);
	u32 wk[2] = { ld32(xmm0, 0), ld32(xmm0, 1) };
	u32 an[2], en[2];
	int i;

	for (i = 0; i < 2; i++) {
		u32 ch = (e & f) ^ (~e & g);
		u32 maj = (a & b) ^ (a & c) ^ (b & c);
		u32 s0 = ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22);
		u32 s1 = ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25);
		u32 t = ch + s1 + wk[i] + h;

		an[i] = t + maj + s0;
		en[i] = t + d;
		h = g; g = f; f = e; e = en[i];
		d = c; c = b; b = a; a = an[i];
	}
	st32(dst, 3, an[1]); st32(dst, 2, an[0]); st32(dst, 1, en[1]); st32(dst, 0, en[0]);
}

static u32 sig0(u32 x) { return ror32(x, 7) ^ ror32(x, 18) ^ (x >> 3); }
static u32 sig1(u32 x) { return ror32(x, 17) ^ ror32(x, 19) ^ (x >> 10); }

static void emu_sha256msg1(u8 *dst, const u8 *src)
{
	u32 w4 = ld32(src, 0), w3 = ld32(dst, 3), w2 = ld32(dst, 2), w1 = ld32(dst, 1), w0 = ld32(dst, 0);

	st32(dst, 3, w3 + sig0(w4)); st32(dst, 2, w2 + sig0(w3));
	st32(dst, 1, w1 + sig0(w2)); st32(dst, 0, w0 + sig0(w1));
}

static void emu_sha256msg2(u8 *dst, const u8 *src)
{
	u32 w14 = ld32(src, 2), w15 = ld32(src, 3);
	u32 w16 = ld32(dst, 0) + sig1(w14);
	u32 w17 = ld32(dst, 1) + sig1(w15);
	u32 w18 = ld32(dst, 2) + sig1(w16);
	u32 w19 = ld32(dst, 3) + sig1(w17);

	st32(dst, 3, w19); st32(dst, 2, w18); st32(dst, 1, w17); st32(dst, 0, w16);
}

/* ---- Hardware XMM/YMM access (entered from notify_die with IF=0) --------- */

#define LOAD_CASE(N) \
	case N: \
		if (len == 32) \
			asm volatile("vmovdqu %%ymm" #N ", %0" : "=m"(*(u8 (*)[32])out)); \
		else \
			asm volatile("movdqu %%xmm" #N ", %0" : "=m"(*(u8 (*)[16])out)); \
		break

static noinline __attribute__((target("avx")))
void hw_vreg_load(int n, int len, u8 *out)
{
	switch (n & 15) {
	LOAD_CASE(0);  LOAD_CASE(1);  LOAD_CASE(2);  LOAD_CASE(3);
	LOAD_CASE(4);  LOAD_CASE(5);  LOAD_CASE(6);  LOAD_CASE(7);
	LOAD_CASE(8);  LOAD_CASE(9);  LOAD_CASE(10); LOAD_CASE(11);
	LOAD_CASE(12); LOAD_CASE(13); LOAD_CASE(14); LOAD_CASE(15);
	}
}

#define STORE_CASE(N) \
	case N: \
		if (vex && len == 32) \
			asm volatile("vmovdqu %0, %%ymm" #N :: "m"(*(const u8 (*)[32])in)); \
		else if (vex) \
			asm volatile("vmovdqu %0, %%xmm" #N :: "m"(*(const u8 (*)[16])in)); \
		else \
			asm volatile("movdqu %0, %%xmm" #N :: "m"(*(const u8 (*)[16])in)); \
		break

static noinline __attribute__((target("avx")))
void hw_vreg_store(int n, int len, const u8 *in, bool vex)
{
	switch (n & 15) {
	STORE_CASE(0);  STORE_CASE(1);  STORE_CASE(2);  STORE_CASE(3);
	STORE_CASE(4);  STORE_CASE(5);  STORE_CASE(6);  STORE_CASE(7);
	STORE_CASE(8);  STORE_CASE(9);  STORE_CASE(10); STORE_CASE(11);
	STORE_CASE(12); STORE_CASE(13); STORE_CASE(14); STORE_CASE(15);
	}
}

/* ---- Instruction decoder & classifier ------------------------------------ */

enum op_kind {
	K_NONE, K_GF2P8MUL, K_GF2P8AFF, K_GF2P8AFFINV, K_VNNI_BUSD, K_VNNI_WSSD, K_VNNI8,
	K_IFMA_L, K_IFMA_H, K_VAES, K_CLMUL,
	K_SHA1NEXTE, K_SHA1MSG1, K_SHA1MSG2, K_SHA256RNDS2, K_SHA256MSG1, K_SHA256MSG2, K_SHA1RNDS4,
};

struct decoded_op {
	enum op_kind kind;
	bool vex;
	int len;          /* 16 or 32 */
	int dst, src1, rm;/* rm < 0 means memory operand at mem_addr */
	int sat, a_signed, b_signed, aes_op;
	u8 imm;
	int insn_len;
	unsigned long mem_addr;
};

static unsigned long get_gpr(const struct pt_regs *regs, int r)
{
	switch (r & 15) {
	case 0:  return regs->ax;
	case 1:  return regs->cx;
	case 2:  return regs->dx;
	case 3:  return regs->bx;
	case 4:  return regs->sp;
	case 5:  return regs->bp;
	case 6:  return regs->si;
	case 7:  return regs->di;
	case 8:  return regs->r8;
	case 9:  return regs->r9;
	case 10: return regs->r10;
	case 11: return regs->r11;
	case 12: return regs->r12;
	case 13: return regs->r13;
	case 14: return regs->r14;
	default: return regs->r15;
	}
}

static bool decode_and_classify(const u8 *buf, int max_len,
				const struct pt_regs *regs,
				struct decoded_op *o)
{
	int pos = 0;
	u8 pp = 0, rex = 0, m = 0, opc = 0, modrm = 0;
	int rex_r = 0, rex_x = 0, rex_b = 0, w = 0, l = 0, vv = 0;
	bool addr32 = false;
	int mod, reg, rm_low;
	bool rip_rel = false;
	unsigned long base_val = 0, index_val = 0;
	s64 disp = 0;

	memset(o, 0, sizeof(*o));

	/* 1. Legacy prefixes */
	while (pos < max_len) {
		u8 b = buf[pos];

		if (b == 0x66) { pp = 1; pos++; }
		else if (b == 0xf3) { pp = 2; pos++; }
		else if (b == 0xf2) { pp = 3; pos++; }
		else if (b == 0x67) { addr32 = true; pos++; }
		else if (b == 0x2e || b == 0x3e || b == 0x26 || b == 0x36 ||
			 b == 0x64 || b == 0x65) { pos++; }
		else if ((b & 0xf0) == 0x40) { rex = b; pos++; break; }
		else break;
	}
	if (pos >= max_len)
		return false;

	/* 2. VEX or 0F 38 / 0F 3A opcode */
	if (!rex && (buf[pos] == 0xc4 || buf[pos] == 0xc5)) {
		o->vex = true;
		if (buf[pos] == 0xc5) {
			if (pos + 3 > max_len)
				return false;
			return false; /* 2-byte VEX implies m=1 (0F), not used here */
		}
		if (pos + 4 > max_len)
			return false;
		rex_r = (~buf[pos + 1] >> 7) & 1;
		rex_x = (~buf[pos + 1] >> 6) & 1;
		rex_b = (~buf[pos + 1] >> 5) & 1;
		m = buf[pos + 1] & 0x1f;
		w = (buf[pos + 2] >> 7) & 1;
		vv = (~buf[pos + 2] >> 3) & 0x0f;
		l = (buf[pos + 2] >> 2) & 1;
		pp = buf[pos + 2] & 0x03;
		opc = buf[pos + 3];
		pos += 4;
	} else if (buf[pos] == 0x0f) {
		if (pos + 3 > max_len)
			return false;
		if (buf[pos + 1] == 0x38)
			m = 2;
		else if (buf[pos + 1] == 0x3a)
			m = 3;
		else
			return false;
		opc = buf[pos + 2];
		if (rex) {
			w = (rex >> 3) & 1;
			rex_r = (rex >> 2) & 1;
			rex_x = (rex >> 1) & 1;
			rex_b = rex & 1;
		}
		pos += 3;
	} else {
		return false;
	}

	/* 3. ModRM + SIB + Displacement */
	if (pos >= max_len)
		return false;
	modrm = buf[pos++];
	mod = (modrm >> 6) & 3;
	reg = ((modrm >> 3) & 7) | (rex_r << 3);
	rm_low = modrm & 7;

	o->len = (o->vex && l) ? 32 : 16;
	o->dst = reg;
	o->src1 = o->vex ? vv : reg;

	if (mod == 3) {
		o->rm = rm_low | (rex_b << 3);
	} else {
		o->rm = -1;
		if (rm_low == 4) {
			u8 sib;
			int scale, idx_reg, base_low, base_reg;

			if (pos >= max_len)
				return false;
			sib = buf[pos++];
			scale = (sib >> 6) & 3;
			idx_reg = ((sib >> 3) & 7) | (rex_x << 3);
			base_low = sib & 7;
			base_reg = base_low | (rex_b << 3);

			if (idx_reg != 4)
				index_val = get_gpr(regs, idx_reg) << scale;
			if (mod == 0 && base_low == 5) {
				if (pos + 4 > max_len)
					return false;
				disp = (s64)(s32)ld32(buf + pos, 0);
				pos += 4;
			} else {
				base_val = get_gpr(regs, base_reg);
			}
		} else if (mod == 0 && rm_low == 5) {
			if (pos + 4 > max_len)
				return false;
			disp = (s64)(s32)ld32(buf + pos, 0);
			pos += 4;
			rip_rel = true;
		} else {
			base_val = get_gpr(regs, rm_low | (rex_b << 3));
		}

		if (mod == 1) {
			if (pos + 1 > max_len)
				return false;
			disp = (s64)(s8)buf[pos++];
		} else if (mod == 2) {
			if (pos + 4 > max_len)
				return false;
			disp = (s64)(s32)ld32(buf + pos, 0);
			pos += 4;
		}
	}

	/* 4. Immediate byte (m == 3) */
	if (m == 3) {
		if (pos >= max_len)
			return false;
		o->imm = buf[pos++];
	}

	o->insn_len = pos;
	if (o->rm < 0) {
		unsigned long ea = rip_rel ? (regs->ip + o->insn_len + disp)
					   : (base_val + index_val + disp);
		o->mem_addr = addr32 ? (ea & 0xffffffffUL) : ea;
	}

	/* 5. Classify opcode */
	if (m == 2) {
		if (o->vex) {
			if (pp == 1 && !w && opc == 0xcf) { o->kind = K_GF2P8MUL; return true; }
			if (pp == 1 && !w && (opc == 0x50 || opc == 0x51)) {
				o->kind = K_VNNI_BUSD; o->sat = opc & 1; return true;
			}
			if (pp == 1 && !w && (opc == 0x52 || opc == 0x53)) {
				o->kind = K_VNNI_WSSD; o->sat = opc & 1; return true;
			}
			if (!w && (opc == 0x50 || opc == 0x51) && pp != 1) {
				o->kind = K_VNNI8; o->sat = opc & 1;
				o->a_signed = (pp == 3 || pp == 2);
				o->b_signed = (pp == 3);
				return true;
			}
			if (pp == 1 && w && (opc == 0xb4 || opc == 0xb5)) {
				o->kind = (opc == 0xb4) ? K_IFMA_L : K_IFMA_H;
				return true;
			}
			if (pp == 1 && l && opc >= 0xdc && opc <= 0xdf) {
				o->kind = K_VAES; o->aes_op = opc - 0xdc; return true;
			}
			return false;
		}
		switch (opc) {
		case 0xc8: o->kind = K_SHA1NEXTE; return true;
		case 0xc9: o->kind = K_SHA1MSG1; return true;
		case 0xca: o->kind = K_SHA1MSG2; return true;
		case 0xcb: o->kind = K_SHA256RNDS2; return true;
		case 0xcc: o->kind = K_SHA256MSG1; return true;
		case 0xcd: o->kind = K_SHA256MSG2; return true;
		case 0xcf: if (pp == 1) { o->kind = K_GF2P8MUL; return true; } return false;
		default: return false;
		}
	}
	if (m == 3) {
		if (o->vex) {
			if (pp == 1 && w && opc == 0xce) { o->kind = K_GF2P8AFF; return true; }
			if (pp == 1 && w && opc == 0xcf) { o->kind = K_GF2P8AFFINV; return true; }
			if (pp == 1 && l && opc == 0x44) { o->kind = K_CLMUL; return true; }
			return false;
		}
		if (opc == 0xcc && pp == 0) { o->kind = K_SHA1RNDS4; return true; }
		if (pp == 1 && opc == 0xce) { o->kind = K_GF2P8AFF; return true; }
		if (pp == 1 && opc == 0xcf) { o->kind = K_GF2P8AFFINV; return true; }
	}
	return false;
}

static int fetch_user_bytes(unsigned long uaddr, u8 *dst, int max_n)
{
	const u8 __user *p = (const u8 __user *)uaddr;
	int i;

	if (uaddr >= TASK_SIZE_MAX)
		return 0;
	#ifdef GBOS_FAULT_RETRY
	pagefault_disable();
	#endif
	__uaccess_begin();
	for (i = 0; i < max_n; i++) {
		u8 b;

		__get_user_asm(b, p + i, "b", "=q", fault);
		dst[i] = b;
	}
fault:
	__uaccess_end();
	#ifdef GBOS_FAULT_RETRY
	pagefault_enable();
	#endif
	return i;
}

static int gbos_isa_emu_die_notify(struct notifier_block *nb,
				   unsigned long val, void *arg)
{
	struct die_args *args = arg;
	struct pt_regs *regs;
	u8 ibuf[15];
	u8 a[32], b[32], d[32];
	struct decoded_op op;
	int nread;

	if (val != DIE_TRAP || !args || args->trapnr != X86_TRAP_UD)
		return NOTIFY_DONE;
	regs = args->regs;
	if (!regs || !user_mode(regs))
		return NOTIFY_DONE;

	nread = fetch_user_bytes(regs->ip, ibuf, sizeof(ibuf));
	if (nread < 3 || !decode_and_classify(ibuf, nread, regs, &op))
		return NOTIFY_DONE;

	if (op.rm < 0) {
		if (fetch_user_bytes(op.mem_addr, b, op.len) != op.len)
			return gbos_retry_user_fault(op.mem_addr, op.len, false, regs->ip);
	} else {
		hw_vreg_load(op.rm, op.len, b);
	}

	hw_vreg_load(op.src1, op.len, a);
	hw_vreg_load(op.dst, op.len, d);

	switch (op.kind) {
	case K_GF2P8MUL:    emu_gf2p8mulb(d, a, b, op.len); break;
	case K_GF2P8AFF:    emu_gf2p8affineqb(d, a, b, op.imm, op.len); break;
	case K_GF2P8AFFINV: emu_gf2p8affineinvqb(d, a, b, op.imm, op.len); break;
	case K_VNNI_BUSD:   emu_vpdpbusd(d, a, b, op.len, op.sat); break;
	case K_VNNI_WSSD:   emu_vpdpwssd(d, a, b, op.len, op.sat); break;
	case K_VNNI8:       emu_vpdp8(d, a, b, op.len, op.sat, op.a_signed, op.b_signed); break;
	case K_IFMA_L:      emu_vpmadd52(d, a, b, op.len, 0); break;
	case K_IFMA_H:      emu_vpmadd52(d, a, b, op.len, 1); break;
	case K_VAES:        emu_vaes(d, a, b, op.len, op.aes_op); break;
	case K_CLMUL:       emu_vpclmulqdq(d, a, b, op.imm, op.len); break;
	case K_SHA1NEXTE:   emu_sha1nexte(d, b); break;
	case K_SHA1MSG1:    emu_sha1msg1(d, b); break;
	case K_SHA1MSG2:    emu_sha1msg2(d, b); break;
	case K_SHA1RNDS4:   emu_sha1rnds4(d, b, op.imm); break;
	case K_SHA256MSG1:  emu_sha256msg1(d, b); break;
	case K_SHA256MSG2:  emu_sha256msg2(d, b); break;
	case K_SHA256RNDS2: {
		u8 x0[16];

		hw_vreg_load(0, 16, x0);
		emu_sha256rnds2(d, b, x0);
		break;
	}
	default:
		return NOTIFY_DONE;
	}

	hw_vreg_store(op.dst, op.len, d, op.vex);
	regs->ip += op.insn_len;
	return NOTIFY_STOP;
}

static struct notifier_block gbos_isa_emu_nb = {
	.notifier_call = gbos_isa_emu_die_notify,
	.priority = 0x7fffffff,
};

static int __init gbos_isa_emu_init(void)
{
	aes_init_tables();
	gbos_user_memory_init();
	return register_die_notifier(&gbos_isa_emu_nb);
}

static void __exit gbos_isa_emu_exit(void)
{
	unregister_die_notifier(&gbos_isa_emu_nb);
	gbos_user_memory_exit();
}

module_init(gbos_isa_emu_init);
module_exit(gbos_isa_emu_exit);
MODULE_DESCRIPTION("Googlebook x86-64 ISA extension trap emulator (AVX-VNNI/IFMA/GFNI/SHA-NI)");
MODULE_LICENSE("GPL");
