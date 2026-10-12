from datetime import datetime, timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from .. import tasks
from ..models import Run, Schedule
from .base import RunTestCase


def local(*args):
    return timezone.make_aware(datetime(*args))


class NextOccurrenceTests(RunTestCase):
    def schedule(self, frequency, starts_at):
        return Schedule(frequency=frequency, starts_at=starts_at)

    def test_before_the_first_run_it_is_the_first_run(self):
        start = local(2026, 10, 14, 9, 0)
        for frequency in Schedule.Frequency:
            self.assertEqual(self.schedule(frequency, start).next_occurrence(local(2026, 10, 1, 0, 0)), start)

    def test_once_has_nothing_after_it_ran(self):
        start = local(2026, 10, 14, 9, 0)
        self.assertIsNone(self.schedule("once", start).next_occurrence(start))

    def test_daily_keeps_the_time_of_day(self):
        schedule = self.schedule("daily", local(2026, 10, 14, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 20, 8, 59)), local(2026, 10, 20, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 20, 9, 0)), local(2026, 10, 21, 9, 0))

    def test_weekly_keeps_the_weekday(self):
        schedule = self.schedule("weekly", local(2026, 10, 14, 9, 0))  # quarta-feira
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 14, 9, 0)), local(2026, 10, 21, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 19, 12, 0)), local(2026, 10, 21, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 30, 12, 0)), local(2026, 11, 4, 9, 0))

    def test_monthly_keeps_the_day_and_falls_back_to_the_last_one(self):
        schedule = self.schedule("monthly", local(2026, 10, 31, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 10, 31, 9, 0)), local(2026, 11, 30, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2026, 12, 31, 10, 0)), local(2027, 1, 31, 9, 0))
        self.assertEqual(schedule.next_occurrence(local(2027, 2, 1, 0, 0)), local(2027, 2, 28, 9, 0))


class RunDueSchedulesTests(RunTestCase):
    def make_schedule(self, **kwargs):
        past = timezone.now() - timedelta(minutes=1)
        defaults = {
            "project": self.project,
            "agent": self.agent,
            "prompt": "Rode os testes",
            "starts_at": past,
            "next_run_at": past,
            "created_by": self.user,
        }
        return Schedule.objects.create(**defaults | kwargs)

    def dispatch(self):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            created = tasks.run_due_schedules()
        return created, delay

    def test_due_schedule_creates_and_enqueues_a_run(self):
        schedule = self.make_schedule(autonomous=True)
        created, delay = self.dispatch()
        run = Run.objects.get()
        self.assertEqual(created, 1)
        delay.assert_called_once_with(run.pk)
        self.assertEqual(
            (run.schedule, run.project, run.agent, run.prompt, run.autonomous, run.created_by, run.status),
            (schedule, self.project, self.agent, "Rode os testes", True, self.user, Run.Status.QUEUED),
        )
        self.assertContains(self.client.get(run.get_absolute_url()), f'href="{schedule.get_absolute_url()}"')
        # Não repete: depois de rodar, não sobra próxima execução
        schedule.refresh_from_db()
        self.assertIsNone(schedule.next_run_at)
        self.assertEqual(self.dispatch()[0], 0)

    def test_recurring_schedule_moves_to_the_next_occurrence(self):
        schedule = self.make_schedule(frequency="daily")
        self.dispatch()
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_run_at, schedule.starts_at + timedelta(days=1))

    def test_future_and_paused_schedules_do_not_run(self):
        self.make_schedule(next_run_at=timezone.now() + timedelta(hours=1))
        self.make_schedule(is_active=False)
        self.assertEqual(self.dispatch()[0], 0)
        self.assertFalse(Run.objects.exists())

    def test_round_is_skipped_while_the_previous_run_is_not_finished(self):
        schedule = self.make_schedule(frequency="daily")
        previous = self.make_run(schedule=schedule, status=Run.Status.WAITING)
        self.assertEqual(self.dispatch()[0], 0)
        schedule.refresh_from_db()
        self.assertEqual(Run.objects.count(), 1)
        self.assertIn("anterior", schedule.skip_reason)
        self.assertGreater(schedule.next_run_at, timezone.now())
        self.assertContains(self.client.get(reverse("orchestrator:schedule_list")), "Pulada em")

        # Com a anterior finalizada, a rodada seguinte sai e o aviso some
        Run.objects.filter(pk=previous.pk).update(status=Run.Status.SUCCEEDED)
        Schedule.objects.update(next_run_at=timezone.now())
        self.assertEqual(self.dispatch()[0], 1)
        schedule.refresh_from_db()
        self.assertEqual((schedule.skipped_at, schedule.skip_reason), (None, ""))

    def test_round_is_skipped_when_the_agent_is_inactive(self):
        schedule = self.make_schedule()
        self.agent.is_active = False
        self.agent.save()
        self.assertEqual(self.dispatch()[0], 0)
        schedule.refresh_from_db()
        self.assertIn("inativo", schedule.skip_reason)
        self.assertFalse(Run.objects.exists())


class ScheduleCrudTests(RunTestCase):
    def data(self, **kwargs):
        defaults = {
            "prompt": "Rode os testes",
            "project": self.project.pk,
            "agent": self.agent.pk,
            "starts_at": "2030-01-07T09:30",
            "frequency": "weekly",
        }
        return defaults | kwargs

    def test_create_edit_pause_and_delete(self):
        self.assertContains(self.client.get(reverse("orchestrator:schedule_list")), "Nenhum agendamento ainda")
        response = self.client.post(reverse("orchestrator:schedule_create"), self.data(), follow=True)
        self.assertRedirects(response, reverse("orchestrator:schedule_list"))
        self.assertContains(response, "07/01/2030 09:30")
        self.assertContains(response, "Toda semana")
        schedule = Schedule.objects.get()
        self.assertEqual(schedule.created_by, self.user)
        self.assertEqual(schedule.next_run_at, local(2030, 1, 7, 9, 30))

        # A tela de edição devolve o horário no formato do campo do navegador
        self.assertContains(self.client.get(schedule.get_absolute_url()), 'value="2030-01-07T09:30"')
        self.client.post(schedule.get_absolute_url(), self.data(starts_at="2030-01-08T10:00", is_active="on"))
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_run_at, local(2030, 1, 8, 10, 0))

        response = self.client.post(schedule.get_absolute_url(), self.data(starts_at="2030-01-08T10:00"), follow=True)
        self.assertContains(response, "Pausado")

        self.client.post(reverse("orchestrator:schedule_delete", args=[schedule.pk]))
        self.assertFalse(Schedule.objects.exists())

    def test_one_time_schedule_must_be_in_the_future(self):
        data = self.data(starts_at="2020-01-01T09:00", frequency="once")
        response = self.client.post(reverse("orchestrator:schedule_create"), data)
        self.assertContains(response, "Esse horário já passou")
        self.assertContains(response, 'value="2020-01-01T09:00"')
        self.assertFalse(Schedule.objects.exists())

    def test_recurring_schedule_may_start_in_the_past(self):
        self.client.post(reverse("orchestrator:schedule_create"), self.data(starts_at="2020-01-01T09:00", frequency="daily"))
        next_run = timezone.localtime(Schedule.objects.get().next_run_at)
        self.assertGreater(next_run, timezone.now())
        self.assertEqual((next_run.hour, next_run.minute), (9, 0))

    def test_deleting_the_schedule_keeps_its_runs(self):
        schedule = Schedule.objects.create(
            project=self.project, agent=self.agent, prompt="x", starts_at=timezone.now(), created_by=self.user
        )
        run = self.make_run(schedule=schedule)
        self.assertContains(self.client.get(schedule.get_absolute_url()), run.get_absolute_url())
        self.client.post(reverse("orchestrator:schedule_delete", args=[schedule.pk]))
        run.refresh_from_db()
        self.assertIsNone(run.schedule)

    def test_project_with_schedule_cannot_be_deleted(self):
        Schedule.objects.create(
            project=self.project, agent=self.agent, prompt="x", starts_at=timezone.now(), created_by=self.user
        )
        response = self.client.post(reverse("orchestrator:project_delete", args=[self.project.pk]), follow=True)
        self.assertContains(response, "tem execuções ou agendamentos")

    def test_list_shows_the_next_to_run_first_and_the_finished_last(self):
        common = {"project": self.project, "agent": self.agent, "prompt": "x", "starts_at": timezone.now(), "created_by": self.user}
        done = Schedule.objects.create(**common)
        later = Schedule.objects.create(**common, next_run_at=timezone.now() + timedelta(days=2))
        sooner = Schedule.objects.create(**common, next_run_at=timezone.now() + timedelta(days=1))
        page = self.client.get(reverse("orchestrator:schedule_list"))
        self.assertEqual(list(page.context["schedules"]), [sooner, later, done])

    def test_section_is_highlighted_in_the_sidebar(self):
        page = self.client.get(reverse("orchestrator:schedule_list"))
        self.assertEqual(page.context["sidebar"]["section"], "schedules")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("orchestrator:schedule_list")).status_code, 302)
