"""
Scheduler Service
Runs briefings on a schedule using APScheduler.
"""

from typing import Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


class BriefingScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Pacific/Auckland")

    def start(self):
        self.scheduler.start()
        print("✓ Scheduler started (NZ/Auckland timezone)")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

    def add_job(
        self,
        profile_id: str,
        schedule: str,   # "daily" | "weekdays" | "weekly"
        time_str: str,   # "07:00"
        callback: Callable,
    ):
        hour, minute = map(int, time_str.split(":"))
        job_id = f"briefing_{profile_id}"

        triggers = {
            "daily":    CronTrigger(hour=hour, minute=minute),
            "weekdays": CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            "weekly":   CronTrigger(day_of_week="mon", hour=hour, minute=minute),
        }
        trigger = triggers.get(schedule, triggers["daily"])

        self.scheduler.add_job(
            callback,
            trigger=trigger,
            id=job_id,
            args=[profile_id],
            replace_existing=True,
            misfire_grace_time=300,  # 5 min grace window
        )
        print(f"✓ Scheduled '{job_id}' → {schedule} at {time_str} NZST")

    def remove_job(self, profile_id: str):
        job_id = f"briefing_{profile_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def clear_all(self):
        self.scheduler.remove_all_jobs()

    def list_jobs(self) -> list[dict]:
        return [
            {
                "id": job.id,
                "next_run": str(job.next_run_time),
                "trigger": str(job.trigger),
            }
            for job in self.scheduler.get_jobs()
        ]
