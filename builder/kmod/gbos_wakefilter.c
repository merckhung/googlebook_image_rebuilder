// SPDX-License-Identifier: GPL-2.0-only
/*
 * gbos_wakefilter: keep a dark resume dark on the KANO.
 *
 * The GKI's ACPI button driver reports KEY_WAKEUP from the "Power Button" input device on every resume
 * (drivers/acpi/button.c, acpi_button_resume). Android maps it to KEYCODE_WAKEUP, a wake key, so every resume turns
 * the screen on. On the KANO the EC's SCI resumes the system from s2idle for routine host events every few seconds
 * to minutes; measured 2026-10-13: screen off or lid closed -> the screen comes back on after 7-76 s, also with the
 * lid closed.
 *
 * A real power-button press during suspend runs acpi_button_notify() -> acpi_pm_wakeup_event() on the button's ACPI
 * device, which bumps the event count of its wakeup source (the KEY_POWER itself is swallowed while suspended, so the
 * KEY_WAKEUP is the only key a press produces). This module drops that resume KEY_WAKEUP only when, since
 * PM_SUSPEND_PREPARE, the power button's wakeup source did not count an event and no other input device reported a
 * key press or a switch change (keyboard, touchpad button, lid). Everything else, including KEY_POWER, passes
 * untouched; with the module not loaded nothing changes.
 */
#include <linux/device.h>
#include <linux/input.h>
#include <linux/module.h>
#include <linux/pm_wakeup.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/suspend.h>

#define ACPI_BUTTON_TYPE_POWER 0x01	/* input id.product of the ACPI power button (drivers/acpi/button.c) */

static DEFINE_SPINLOCK(wf_lock);
static struct input_dev *wf_button;	/* the ACPI power button, if connected */
static bool wf_armed;			/* a suspend was prepared and its resume KEY_WAKEUP not seen yet */
static bool wf_user;			/* a press or a switch change from another device since PM_SUSPEND_PREPARE */
static bool wf_dropping;		/* the current KEY_WAKEUP press is being dropped (drop its release too) */
static unsigned long wf_count;		/* the power button's wakeup event count at PM_SUSPEND_PREPARE */
static unsigned int wf_dropped;
module_param_named(dropped, wf_dropped, uint, 0444);
MODULE_PARM_DESC(dropped, "resume KEY_WAKEUP events dropped so far");

static bool is_power_button(struct input_dev *dev)
{
	return dev->id.bustype == BUS_HOST && dev->id.product == ACPI_BUTTON_TYPE_POWER && dev->phys &&
	       (!strncmp(dev->phys, "LNXPWRBN/", 9) || !strncmp(dev->phys, "PNP0C0C/", 8));
}

/* event count of the wakeup source of the button's parent (its ACPI device); 0 if it has none */
static unsigned long button_events(struct input_dev *dev)
{
	struct device *parent = dev ? dev->dev.parent : NULL;

	return parent && parent->power.wakeup ? READ_ONCE(parent->power.wakeup->event_count) : 0;
}

static bool wf_filter(struct input_handle *handle, unsigned int type, unsigned int code, int value)
{
	unsigned long flags;
	bool drop = false;

	if (handle->dev != wf_button) {
		if ((type == EV_KEY && value == 1) || type == EV_SW) {
			spin_lock_irqsave(&wf_lock, flags);
			wf_user = true;
			spin_unlock_irqrestore(&wf_lock, flags);
		}
		return false;
	}
	if (type != EV_KEY || code != KEY_WAKEUP)
		return false;

	spin_lock_irqsave(&wf_lock, flags);
	if (value == 1) {
		wf_dropping = wf_armed && !wf_user && button_events(handle->dev) == wf_count;
		wf_armed = false;
		if (wf_dropping)
			wf_dropped++;
	}
	drop = wf_dropping;
	if (value == 0)
		wf_dropping = false;
	spin_unlock_irqrestore(&wf_lock, flags);
	return drop;
}

static int wf_pm(struct notifier_block *nb, unsigned long action, void *data)
{
	unsigned long flags;

	if (action != PM_SUSPEND_PREPARE)
		return NOTIFY_DONE;
	spin_lock_irqsave(&wf_lock, flags);
	wf_count = button_events(wf_button);
	wf_user = false;
	wf_armed = wf_button != NULL;
	spin_unlock_irqrestore(&wf_lock, flags);
	return NOTIFY_DONE;
}

static struct notifier_block wf_pm_nb = { .notifier_call = wf_pm };

static int wf_connect(struct input_handler *handler, struct input_dev *dev, const struct input_device_id *id)
{
	struct input_handle *handle;
	unsigned long flags;
	int error;

	handle = kzalloc(sizeof(*handle), GFP_KERNEL);
	if (!handle)
		return -ENOMEM;
	handle->dev = dev;
	handle->handler = handler;
	handle->name = "gbos_wakefilter";
	error = input_register_handle(handle);
	if (error)
		goto err_free;
	error = input_open_device(handle);
	if (error)
		goto err_unregister;
	if (is_power_button(dev)) {
		spin_lock_irqsave(&wf_lock, flags);
		if (!wf_button)
			wf_button = dev;
		spin_unlock_irqrestore(&wf_lock, flags);
		dev_info(&dev->dev, "gbos_wakefilter: watching %s (%s)\n", dev->name, dev->phys);
	}
	return 0;

err_unregister:
	input_unregister_handle(handle);
err_free:
	kfree(handle);
	return error;
}

static void wf_disconnect(struct input_handle *handle)
{
	unsigned long flags;

	spin_lock_irqsave(&wf_lock, flags);
	if (handle->dev == wf_button) {
		wf_button = NULL;
		wf_armed = false;
		wf_dropping = false;
	}
	spin_unlock_irqrestore(&wf_lock, flags);
	input_close_device(handle);
	input_unregister_handle(handle);
	kfree(handle);
}

static const struct input_device_id wf_ids[] = {
	{ .flags = INPUT_DEVICE_ID_MATCH_EVBIT, .evbit = { BIT_MASK(EV_KEY) } },
	{ .flags = INPUT_DEVICE_ID_MATCH_EVBIT, .evbit = { BIT_MASK(EV_SW) } },
	{ },
};
MODULE_DEVICE_TABLE(input, wf_ids);

static struct input_handler wf_handler = {
	.filter = wf_filter,
	.connect = wf_connect,
	.disconnect = wf_disconnect,
	.name = "gbos_wakefilter",
	.id_table = wf_ids,
};

static int __init wf_init(void)
{
	int error = register_pm_notifier(&wf_pm_nb);

	if (error)
		return error;
	error = input_register_handler(&wf_handler);
	if (error)
		unregister_pm_notifier(&wf_pm_nb);
	return error;
}

static void __exit wf_exit(void)
{
	input_unregister_handler(&wf_handler);
	unregister_pm_notifier(&wf_pm_nb);
}

module_init(wf_init);
module_exit(wf_exit);
MODULE_DESCRIPTION("Drop the resume KEY_WAKEUP of the ACPI power button when it did not wake the system");
MODULE_LICENSE("GPL");
