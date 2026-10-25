/* SPDX-License-Identifier: GPL-2.0 */
/* Demand-page operands on a worker, then retry the original #UD.
 * The notifier runs with IRQs disabled and must never take a page fault.
 * No vector registers are touched while work can sleep or be preempted.
 */
#ifndef GBOS_ISA_USER_MEMORY_H
#define GBOS_ISA_USER_MEMORY_H
#ifdef GBOS_FAULT_RETRY
#include <linux/mm.h>
#include <linux/spinlock.h>
#include <linux/kthread.h>
#include <linux/sched/mm.h>
#include <linux/workqueue.h>

#define GBOS_FAULT_SLOTS 128
struct gbos_user_fault {
	struct work_struct work;
	struct mm_struct *mm;
	unsigned long ip, address;
	u64 birth;
	pid_t tid;
	unsigned int length;
	bool write;
	enum { GBOS_FREE, GBOS_QUEUED, GBOS_FAILED } state;
};
static struct gbos_user_fault gbos_faults[GBOS_FAULT_SLOTS];
static DEFINE_SPINLOCK(gbos_fault_lock);

static void gbos_fault_work(struct work_struct *work)
{
	struct gbos_user_fault *f = container_of(work, struct gbos_user_fault, work);
	struct page *pages[2];
	struct mm_struct *mm = f->mm;
	unsigned long flags;
	int count = DIV_ROUND_UP((f->address & ~PAGE_MASK) + f->length, PAGE_SIZE);
	long pinned = 0;

	kthread_use_mm(mm);
	pinned = pin_user_pages_fast(f->address & PAGE_MASK, count,
				    f->write ? FOLL_WRITE : 0, pages);
	if (pinned > 0)
		unpin_user_pages(pages, pinned);
	kthread_unuse_mm(mm);
	mmput(mm);
	spin_lock_irqsave(&gbos_fault_lock, flags);
	f->state = pinned == count ? GBOS_FREE : GBOS_FAILED;
	spin_unlock_irqrestore(&gbos_fault_lock, flags);
	module_put(THIS_MODULE);
}

static int gbos_retry_user_fault(unsigned long address, unsigned int length,
				bool write, unsigned long ip)
{
	struct gbos_user_fault *slot = NULL, *old_failure = NULL;
	struct mm_struct *mm;
	unsigned long flags;
	int i, ret;

	if (!length || length > 64 || address >= TASK_SIZE_MAX ||
	    length > TASK_SIZE_MAX - address || (current->flags & PF_EXITING))
		return NOTIFY_DONE;
	spin_lock_irqsave(&gbos_fault_lock, flags);
	for (i = 0; i < GBOS_FAULT_SLOTS; i++) {
		struct gbos_user_fault *f = &gbos_faults[i];
        if (f->state == GBOS_QUEUED && f->tid == current->pid &&
            f->birth == current->start_boottime && f->ip == ip &&
            f->address == address && f->write == write) {
            spin_unlock_irqrestore(&gbos_fault_lock, flags);
            return NOTIFY_STOP;
        }
		if (f->state == GBOS_FAILED) {
			if (f->tid == current->pid && f->birth == current->start_boottime &&
			    f->ip == ip && f->address == address && f->write == write) {
				f->state = GBOS_FREE;
				spin_unlock_irqrestore(&gbos_fault_lock, flags);
				return NOTIFY_DONE;
			}
			old_failure = f;
		}
		if (!slot && f->state == GBOS_FREE)
			slot = f;
	}
	if (!slot)
		slot = old_failure;
	if (!slot || !try_module_get(THIS_MODULE)) {
		spin_unlock_irqrestore(&gbos_fault_lock, flags);
		return NOTIFY_DONE;
	}
	slot->state = GBOS_QUEUED;
	slot->tid = current->pid;
	slot->birth = current->start_boottime;
	slot->ip = ip;
	slot->address = address;
	slot->length = length;
	slot->write = write;
	mm = current->mm;
	slot->mm = mm;
	mmget(mm);
	ret = queue_work(system_unbound_wq, &slot->work) ? 0 : -1;
	if (ret)
		slot->state = GBOS_FREE;
	spin_unlock_irqrestore(&gbos_fault_lock, flags);
	if (ret) {
		mmput(mm);
		module_put(THIS_MODULE);
		return NOTIFY_DONE;
	}
	return NOTIFY_STOP;
}
static void gbos_user_memory_init(void)
{
    int i;
    for (i = 0; i < GBOS_FAULT_SLOTS; i++)
        INIT_WORK(&gbos_faults[i].work, gbos_fault_work);
}
static void gbos_user_memory_exit(void)
{
    int i;
    for (i = 0; i < GBOS_FAULT_SLOTS; i++)
        cancel_work_sync(&gbos_faults[i].work);
}
#else
static inline void gbos_user_memory_init(void) { }
static inline void gbos_user_memory_exit(void) { }
static inline int gbos_retry_user_fault(unsigned long address, unsigned int length,
				       bool write, unsigned long ip)
{
	return NOTIFY_DONE;
}
#endif
#endif
