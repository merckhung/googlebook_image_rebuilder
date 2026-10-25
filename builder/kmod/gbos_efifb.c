// SPDX-License-Identifier: GPL-2.0
/*
 * gbos_efifb: the linear framebuffer the UEFI firmware set up through GOP, exposed as a one-output DRM/KMS device.
 *
 * Googlebook's kernel (GKI 6.18, build 16302624) has no simpledrm/efidrm/fbdev and does not export screen_info, so the
 * framebuffer geometry comes in as module parameters, read by vendor init from /sys/kernel/boot_params/data (the
 * screen_info the EFI stub filled in). The DRM driver is named "vkms" so the stock minigbm uses its dumb-buffer
 * backend; every atomic update copies the XRGB8888 client buffer into the firmware framebuffer. vblank is simulated
 * with an hrtimer at 60 Hz, as in upstream vkms.
 *
 * Two DRM devices are registered: card0 only allocates (minigbm opens the first card node and, as its first opener,
 * becomes DRM master of it), card1 scans out (the composer HAL opens it first, so it can be master; vendor property
 * vendor.hwc.drm.device=/dev/dri/card1). Buffers move from card0 to card1 as dma-bufs (shmem, imported without mapping).
 *
 * Built out of tree against the kernel's own headers (kheaders) and modversions (vmlinux __kcrctab); see build.sh.
 * No indirect calls are made from this file: under kCFI with boot-time hash randomisation only the function preambles
 * listed in .cfi_sites are rewritten, not call sites in an out-of-tree module.
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/hrtimer.h>
#include <linux/io.h>
#include <linux/iosys-map.h>

#include <drm/drm_atomic.h>
#include <drm/drm_atomic_helper.h>
#include <drm/drm_connector.h>
#include <drm/drm_crtc.h>
#include <drm/drm_drv.h>
#include <drm/drm_encoder.h>
#include <drm/drm_fourcc.h>
#include <drm/drm_framebuffer.h>
#include <drm/drm_gem_atomic_helper.h>
#include <drm/drm_gem_framebuffer_helper.h>
#include <drm/drm_gem_shmem_helper.h>
#include <drm/drm_modes.h>
#include <drm/drm_modeset_helper_vtables.h>
#include <drm/drm_plane.h>
#include <drm/drm_probe_helper.h>
#include <drm/drm_simple_kms_helper.h>
#include <drm/drm_vblank.h>

static unsigned long fb_base;
static unsigned int fb_width, fb_height, fb_stride, fb_bpp = 32, fb_red_pos = 16;
module_param(fb_base, ulong, 0444);
module_param(fb_width, uint, 0444);
module_param(fb_height, uint, 0444);
module_param(fb_stride, uint, 0444);
module_param(fb_bpp, uint, 0444);
module_param(fb_red_pos, uint, 0444);

#define GBOS_FRAME_NS 16666667

struct gbos_dev {
	struct drm_device drm;
	void *vram;
	size_t vram_size;
	struct drm_plane plane;
	struct drm_crtc crtc;
	struct drm_encoder encoder;
	struct drm_connector connector;
	struct hrtimer vblank_timer;
	bool scanout;
};

static struct platform_device *gbos_pdev[2];

static struct gbos_dev *gbos_of(struct drm_device *drm)
{
	return container_of(drm, struct gbos_dev, drm);
}

static void gbos_blit(struct gbos_dev *g, struct drm_framebuffer *fb)
{
	struct iosys_map map[DRM_FORMAT_MAX_PLANES];
	struct iosys_map data[DRM_FORMAT_MAX_PLANES];
	u32 x, y, w, h, src_red;
	const u8 *src;

	if (!g->scanout || !fb || fb->format->cpp[0] != 4)
		return;
	memset(map, 0, sizeof(map));
	memset(data, 0, sizeof(data));
	if (drm_gem_fb_begin_cpu_access(fb, DMA_FROM_DEVICE))
		return;
	if (drm_gem_fb_vmap(fb, map, data)) {
		drm_gem_fb_end_cpu_access(fb, DMA_FROM_DEVICE);
		return;
	}
	if (map[0].is_iomem || !map[0].vaddr)
		goto out;
	src = map[0].vaddr;
	/* XRGB/ARGB8888 keep red in bits 16-23; Android's RGBA_8888 is DRM ABGR8888 (red in bits 0-7) */
	src_red = (fb->format->format == DRM_FORMAT_XRGB8888 || fb->format->format == DRM_FORMAT_ARGB8888) ? 16 : 0;
	w = min(fb->width, fb_width);
	h = min(fb->height, fb_height);
	for (y = 0; y < h; y++) {
		const u32 *s = (const u32 *)(src + (size_t)y * fb->pitches[0]);
		u32 *d = (u32 *)((u8 *)g->vram + (size_t)y * fb_stride);

		if (src_red == fb_red_pos) {
			memcpy(d, s, (size_t)w * 4);
		} else {
			/* source and firmware framebuffer disagree on the red/blue order: swap them */
			for (x = 0; x < w; x++) {
				u32 p = s[x];

				d[x] = (p & 0xff00ff00) | ((p >> 16) & 0xff) | ((p & 0xff) << 16);
			}
		}
	}
out:
	drm_gem_fb_vunmap(fb, map);
	drm_gem_fb_end_cpu_access(fb, DMA_FROM_DEVICE);
}

static void gbos_plane_update(struct drm_plane *plane, struct drm_atomic_state *state)
{
	struct drm_plane_state *ps = drm_atomic_get_new_plane_state(state, plane);

	if (ps && ps->fb && ps->visible)
		gbos_blit(gbos_of(plane->dev), ps->fb);
}

static int gbos_plane_check(struct drm_plane *plane, struct drm_atomic_state *state)
{
	struct drm_plane_state *ps = drm_atomic_get_new_plane_state(state, plane);
	struct drm_crtc_state *cs;

	if (!ps->crtc || !ps->fb)
		return 0;
	cs = drm_atomic_get_new_crtc_state(state, ps->crtc);
	return drm_atomic_helper_check_plane_state(ps, cs, DRM_PLANE_NO_SCALING, DRM_PLANE_NO_SCALING, false, true);
}

static const struct drm_plane_helper_funcs gbos_plane_helpers = {
	.prepare_fb = drm_gem_plane_helper_prepare_fb,
	.atomic_check = gbos_plane_check,
	.atomic_update = gbos_plane_update,
};

static struct drm_plane_state *gbos_plane_dup(struct drm_plane *plane)
{
	struct drm_plane_state *s = kzalloc(sizeof(*s), GFP_KERNEL);

	if (s)
		__drm_atomic_helper_plane_duplicate_state(plane, s);
	return s;
}

static void gbos_plane_destroy_state(struct drm_plane *plane, struct drm_plane_state *state)
{
	__drm_atomic_helper_plane_destroy_state(state);
	kfree(state);
}

static const struct drm_plane_funcs gbos_plane_funcs = {
	.update_plane = drm_atomic_helper_update_plane,
	.disable_plane = drm_atomic_helper_disable_plane,
	.destroy = drm_plane_cleanup,
	.reset = drm_atomic_helper_plane_reset,
	.atomic_duplicate_state = gbos_plane_dup,
	.atomic_destroy_state = gbos_plane_destroy_state,
};

static enum hrtimer_restart gbos_vblank_tick(struct hrtimer *t)
{
	struct gbos_dev *g = container_of(t, struct gbos_dev, vblank_timer);

	hrtimer_forward(t, ktime_get(), ns_to_ktime(GBOS_FRAME_NS));
	drm_crtc_handle_vblank(&g->crtc);
	return HRTIMER_RESTART;
}

static int gbos_enable_vblank(struct drm_crtc *crtc)
{
	struct gbos_dev *g = gbos_of(crtc->dev);

	hrtimer_start_range_ns(&g->vblank_timer, ns_to_ktime(GBOS_FRAME_NS), 0, HRTIMER_MODE_REL);
	return 0;
}

static void gbos_disable_vblank(struct drm_crtc *crtc)
{
	hrtimer_cancel(&gbos_of(crtc->dev)->vblank_timer);
}

static void gbos_crtc_enable(struct drm_crtc *crtc, struct drm_atomic_state *state)
{
	drm_crtc_vblank_on(crtc);
}

static void gbos_crtc_disable(struct drm_crtc *crtc, struct drm_atomic_state *state)
{
	drm_crtc_vblank_off(crtc);
}

static void gbos_crtc_flush(struct drm_crtc *crtc, struct drm_atomic_state *state)
{
	struct drm_pending_vblank_event *ev = crtc->state->event;

	if (!ev)
		return;
	crtc->state->event = NULL;
	spin_lock_irq(&crtc->dev->event_lock);
	if (drm_crtc_vblank_get(crtc) == 0)
		drm_crtc_arm_vblank_event(crtc, ev);
	else
		drm_crtc_send_vblank_event(crtc, ev);
	spin_unlock_irq(&crtc->dev->event_lock);
}

static const struct drm_crtc_helper_funcs gbos_crtc_helpers = {
	.atomic_enable = gbos_crtc_enable,
	.atomic_disable = gbos_crtc_disable,
	.atomic_flush = gbos_crtc_flush,
};

static const struct drm_crtc_funcs gbos_crtc_funcs = {
	.reset = drm_atomic_helper_crtc_reset,
	.destroy = drm_crtc_cleanup,
	.set_config = drm_atomic_helper_set_config,
	.page_flip = drm_atomic_helper_page_flip,
	.atomic_duplicate_state = drm_atomic_helper_crtc_duplicate_state,
	.atomic_destroy_state = drm_atomic_helper_crtc_destroy_state,
	.enable_vblank = gbos_enable_vblank,
	.disable_vblank = gbos_disable_vblank,
};

/* the one mode is the firmware's: GOP geometry, 60 Hz */
static const struct drm_display_mode gbos_mode_template = {
	DRM_MODE("", DRM_MODE_TYPE_DRIVER, 65000, 1024, 1032, 1040, 1056, 0, 768, 769, 770, 771, 0, 0)
};

static int gbos_get_modes(struct drm_connector *connector)
{
	struct drm_display_mode *m = drm_mode_duplicate(connector->dev, &gbos_mode_template);

	if (!m)
		return 0;
	m->hdisplay = fb_width;
	m->hsync_start = fb_width + 8;
	m->hsync_end = fb_width + 16;
	m->htotal = fb_width + 32;
	m->vdisplay = fb_height;
	m->vsync_start = fb_height + 1;
	m->vsync_end = fb_height + 2;
	m->vtotal = fb_height + 3;
	m->clock = (int)div_u64((u64)m->htotal * m->vtotal * 60, 1000);
	m->type = DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED;
	drm_mode_set_name(m);
	drm_mode_probed_add(connector, m);
	return 1;
}

static const struct drm_connector_helper_funcs gbos_conn_helpers = {
	.get_modes = gbos_get_modes,
};

static const struct drm_connector_funcs gbos_conn_funcs = {
	.reset = drm_atomic_helper_connector_reset,
	.fill_modes = drm_helper_probe_single_connector_modes,
	.destroy = drm_connector_cleanup,
	.atomic_duplicate_state = drm_atomic_helper_connector_duplicate_state,
	.atomic_destroy_state = drm_atomic_helper_connector_destroy_state,
};

static const struct drm_mode_config_funcs gbos_mode_funcs = {
	.fb_create = drm_gem_fb_create,
	.atomic_check = drm_atomic_helper_check,
	.atomic_commit = drm_atomic_helper_commit,
};

DEFINE_DRM_GEM_FOPS(gbos_fops);

static const struct drm_driver gbos_driver = {
	.driver_features = DRIVER_GEM | DRIVER_MODESET | DRIVER_ATOMIC,
	.name = "vkms",
	.desc = "gbos GOP framebuffer",
	.major = 1,
	.minor = 0,
	.fops = &gbos_fops,
	DRM_GEM_SHMEM_DRIVER_OPS,	/* includes .gem_prime_import = drm_gem_shmem_prime_import_no_map */
};

static int gbos_modeset(struct gbos_dev *g)
{
	struct drm_device *drm = &g->drm;
	static const u32 formats[] = { DRM_FORMAT_XRGB8888, DRM_FORMAT_ARGB8888, DRM_FORMAT_XBGR8888, DRM_FORMAT_ABGR8888 };
	int ret;

	ret = drmm_mode_config_init(drm);
	if (ret)
		return ret;
	drm->mode_config.min_width = fb_width;
	drm->mode_config.min_height = fb_height;
	drm->mode_config.max_width = fb_width;
	drm->mode_config.max_height = fb_height;
	drm->mode_config.preferred_depth = 24;
	drm->mode_config.funcs = &gbos_mode_funcs;

	ret = drm_universal_plane_init(drm, &g->plane, 0x1, &gbos_plane_funcs, formats, ARRAY_SIZE(formats), NULL,
				       DRM_PLANE_TYPE_PRIMARY, NULL);
	if (ret)
		return ret;
	drm_plane_helper_add(&g->plane, &gbos_plane_helpers);
	ret = drm_crtc_init_with_planes(drm, &g->crtc, &g->plane, NULL, &gbos_crtc_funcs, NULL);
	if (ret)
		return ret;
	drm_crtc_helper_add(&g->crtc, &gbos_crtc_helpers);
	ret = drm_simple_encoder_init(drm, &g->encoder, DRM_MODE_ENCODER_VIRTUAL);
	if (ret)
		return ret;
	g->encoder.possible_crtcs = drm_crtc_mask(&g->crtc);
	ret = drm_connector_init(drm, &g->connector, &gbos_conn_funcs, DRM_MODE_CONNECTOR_VIRTUAL);
	if (ret)
		return ret;
	drm_connector_helper_add(&g->connector, &gbos_conn_helpers);
	ret = drm_connector_attach_encoder(&g->connector, &g->encoder);
	if (ret)
		return ret;
	ret = drm_vblank_init(drm, 1);
	if (ret)
		return ret;
	drm_mode_config_reset(drm);
	return 0;
}

static int gbos_probe(struct platform_device *pdev)
{
	struct gbos_dev *g;
	int ret;

	g = devm_drm_dev_alloc(&pdev->dev, &gbos_driver, struct gbos_dev, drm);
	if (IS_ERR(g))
		return PTR_ERR(g);
	hrtimer_setup(&g->vblank_timer, gbos_vblank_tick, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	g->scanout = pdev->id == 1;
	if (g->scanout) {
		g->vram_size = (size_t)fb_stride * fb_height;
		g->vram = memremap(fb_base, g->vram_size, MEMREMAP_WC);
		if (!g->vram)
			return -ENOMEM;
	}
	platform_set_drvdata(pdev, g);
	ret = gbos_modeset(g);
	if (ret)
		goto unmap;
	ret = drm_dev_register(&g->drm, 0);
	if (ret)
		goto unmap;
	if (g->scanout)
		_printk(KERN_INFO "gbos_efifb: %ux%u stride %u at 0x%lx (%s), registered as vkms (scanout)\n", fb_width,
			fb_height, fb_stride, fb_base, fb_red_pos == 16 ? "XRGB" : "XBGR");
	else
		_printk(KERN_INFO "gbos_efifb: allocator device registered as vkms\n");
	return 0;
unmap:
	if (g->vram)
		memunmap(g->vram);
	return ret;
}

static void gbos_remove(struct platform_device *pdev)
{
	struct gbos_dev *g = platform_get_drvdata(pdev);

	drm_dev_unregister(&g->drm);
	drm_atomic_helper_shutdown(&g->drm);
	hrtimer_cancel(&g->vblank_timer);
	if (g->vram)
		memunmap(g->vram);
}

static struct platform_driver gbos_platform = {
	.driver = { .name = "gbos_efifb" },
	.probe = gbos_probe,
	.remove = gbos_remove,
};

static int gbos_init(void)
{
	struct platform_device_info info = { .name = "gbos_efifb" };
	int ret, i;

	if (!fb_base || fb_width < 320 || fb_height < 200 || fb_width > 8192 || fb_height > 8192 || fb_bpp != 32 ||
	    fb_stride < fb_width * 4 || fb_stride > 8192 * 4 || (fb_red_pos != 16 && fb_red_pos != 0)) {
		_printk(KERN_ERR "gbos_efifb: unusable framebuffer base 0x%lx %ux%u stride %u bpp %u red %u\n", fb_base,
			fb_width, fb_height, fb_stride, fb_bpp, fb_red_pos);
		return -ENODEV;
	}
	/*
	 * A GOP framebuffer is page aligned, above the first MiB (legacy VGA, BIOS and ACPI tables) and never in the
	 * window 0xfec00000-0xffffffff (IOAPIC, HPET, local APIC and the memory-mapped firmware flash). A wrong
	 * screen_info or a hand-typed insmod line is refused before memremap() maps anything.
	 */
	if (fb_base & (PAGE_SIZE - 1) || fb_base < 0x100000UL ||
	    (fb_base < 0x100000000UL && fb_base + (unsigned long)fb_stride * fb_height > 0xfec00000UL)) {
		_printk(KERN_ERR "gbos_efifb: framebuffer base 0x%lx (%u bytes) is not where a GOP framebuffer can be\n",
			fb_base, fb_stride * fb_height);
		return -ENODEV;
	}
	ret = __platform_driver_register(&gbos_platform, THIS_MODULE);
	if (ret)
		return ret;
	/* id 0 (allocator) registers first and becomes card0, id 1 (scanout) card1 */
	for (i = 0; i < 2; i++) {
		info.id = i;
		gbos_pdev[i] = platform_device_register_full(&info);
		if (IS_ERR(gbos_pdev[i])) {
			ret = PTR_ERR(gbos_pdev[i]);
			while (--i >= 0)
				platform_device_unregister(gbos_pdev[i]);
			platform_driver_unregister(&gbos_platform);
			return ret;
		}
	}
	return 0;
}

static void gbos_exit(void)
{
	platform_device_unregister(gbos_pdev[1]);
	platform_device_unregister(gbos_pdev[0]);
	platform_driver_unregister(&gbos_platform);
}

__attribute__((section(".text"), used, hot))
int init_module(void)
{
	return gbos_init();
}

__attribute__((section(".text"), used, hot))
void cleanup_module(void)
{
	gbos_exit();
}

struct module __this_module
	__attribute__((section(".gnu.linkonce.this_module"), aligned(64), used)) = {
		.name = "gbos_efifb",
		.init = init_module,
		.exit = cleanup_module,
	};

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("UEFI GOP framebuffer as a DRM device for Googlebook OS");
MODULE_INFO(name, "gbos_efifb");
#ifndef GBOS_VERMAGIC
#define GBOS_VERMAGIC "6.18.21-android17-5-g3938c8d97142-ab16302624 SMP preempt mod_unload modversions "
#endif
MODULE_INFO(vermagic, GBOS_VERMAGIC);
MODULE_INFO(retpoline, "Y");
