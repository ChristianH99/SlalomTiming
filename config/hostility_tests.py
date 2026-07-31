"""What happens when a client sends something the app never expected.

The shipped suite was strong on behaviour and weak on hostility: it
proved at length what the app does when it is driven correctly, and almost
nothing about what it does when it is not. That is the gap the malformed-id
500 lived in —
`filter(id="abc")` raises inside Django's query preparation, which is a 500, and
it was reachable on **nine** endpoints at once. Nine, because nobody had written
the test that asks all of them the same question.

So the tests here do not name endpoints. They **find** them: every JSON endpoint
in the app is discovered from the URLconf, and each is asked every hostile
question. An endpoint added next month is covered the day it is added, which is
the only version of this test worth having — the failure mode being guarded
against is precisely "the tenth endpoint".

`config/tests.py` is the deployment file (things that only break with DEBUG off);
this is its sibling for things that only break when somebody is unkind.
"""

import inspect
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

VIEWS = Path(settings.BASE_DIR) / 'apps'


def _json_endpoints():
    """Every (url_name, path) whose view reads a JSON body.

    Discovered by asking each view's own source whether it parses `request.body`
    — directly or through the app's `_json_body` helper — rather than from a list
    somebody has to remember to extend.
    """
    found = []

    def reads_json(callback):
        """Whether this view parses a JSON request body.

        Read from the view's own source. A class-based view hands `as_view()`
        back, so the class it came from is what has to be read — `view_class` is
        the attribute Django leaves behind for exactly this.
        """
        target = getattr(callback, 'view_class', callback)
        try:
            source = inspect.getsource(target)
        except (OSError, TypeError):
            return False
        return 'request.body' in source or 'json_body(request)' in source

    def walk(resolver, prefix, app_name):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                walk(entry, prefix + str(entry.pattern), entry.app_name or app_name)
            elif isinstance(entry, URLPattern) and entry.name:
                route = prefix + str(entry.pattern)
                # Only argument-free routes: an endpoint taking a pk in the URL
                # is a different question (and Django's own converter refuses a
                # non-numeric one before the view is reached).
                if '<' in route:
                    continue
                if reads_json(entry.callback):
                    found.append((f'{app_name}:{entry.name}', '/' + route))

    walk(get_resolver(), '', '')
    return sorted(set(found))


ENDPOINTS = _json_endpoints()

# Values a client can send that the app has no business trusting. Each one is a
# real bug that reached production code in this repository or a near neighbour
# of one:
HOSTILE = {
    'a word where a number goes': 'abc',
    # isdigit() is True for this and int() raises — the isdigit-then-int pair is
    # what _digits() exists to replace.
    'a Unicode digit': '²',
    # SQLite stores what it is handed; a PositiveIntegerField does not save it.
    'an integer past the column': 10 ** 30,
    'a negative id': -1,
    'a float where an id goes': 1.5,
    'an empty string': '',
    'null': None,
    'a list where a scalar goes': [1, 2, 3],
    'an object where a scalar goes': {'x': 1},
    'a very long string': 'x' * 5000,
}

# The keys these endpoints read. Sending every hostile value under every key at
# once is the point: a view that validates `run_id` and forgets `post` fails.
KEYS = [
    'run_id', 'post', 'slot_key', 'class_key', 'participant', 'bib',
    'bib_number', 'run_number', 'running_number', 'port', 'status', 'value',
    'pylons', 'stop_line', 'task', 'count', 'order', 'scope', 'members',
    'competition', 'time', 'run_time', 'seconds', 'token', 'number', 'dsq',
    'ranks', 'confirm', 'enabled', 'side',
]


@pytest.fixture
def event(db):
    """An active competition with a running class and one starter.

    Without it most of these endpoints answer "no competition is selected" and
    return before they parse anything — so the sweep would pass by never
    reaching the code it is about. This is the difference between a test that
    covers the endpoints and one that only proves they are routed.
    """
    import datetime

    from apps.competitions.models import Competition, CompetitionClass, CompetitionType
    from apps.participants.models import EventEntry, Participant
    from apps.timing.models import TimingSettings

    ctype = CompetitionType.objects.create(name='Slalom')
    competition = Competition.objects.create(
        competition_type=ctype, name='Hostility', date=datetime.date(2026, 5, 1),
        is_active=True)
    # A new competition seeds its own default classes, so this takes one of
    # those rather than adding a second class called "1".
    cclass = competition.classes.get(name='1')
    cclass.is_running = True
    cclass.counted_runs = 2
    cclass.run_position = 0
    cclass.scoring_method = CompetitionClass.Scoring.AGGREGATE
    cclass.save()
    person = Participant.objects.create(
        competition_type=ctype, first_name='A', last_name='B',
        date_of_birth=datetime.date(2000, 1, 1))
    EventEntry.objects.create(competition=competition, participant=person, bib_number=1)
    # timing:signal only accepts posts while the simulator is the chosen device.
    settings_row = TimingSettings.load()
    settings_row.device = TimingSettings.Device.SIMULATOR
    settings_row.save()
    return {'competition': competition, 'cclass': cclass, 'participant': person}


@pytest.mark.django_db
class TestNoEndpointCanBeMadeToCrash:
    """A 400 is an answer. A 500 is the app falling over, and on a timing rig
    mid-event it is the operator's screen going blank."""

    def test_there_are_endpoints_to_test(self):
        """If the discovery breaks, every test below passes vacuously — which is
        the one way a sweep like this can rot without anybody noticing."""
        assert len(ENDPOINTS) >= 15, ENDPOINTS

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    @pytest.mark.parametrize('label', sorted(HOSTILE), ids=sorted(HOSTILE))
    def test_a_hostile_payload_is_refused_not_crashed(self, client, event, name, path, label):
        payload = {key: HOSTILE[label] for key in KEYS}
        response = client.post(path, data=json.dumps(payload),
                               content_type='application/json')
        assert response.status_code < 500, (
            f'{name} answered {response.status_code} to {label}'
        )

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    def test_a_body_that_is_not_json_is_refused_not_crashed(self, client, event, name, path):
        response = client.post(path, data=b'\x00\x01not json at all',
                               content_type='application/json')
        assert response.status_code < 500, f'{name} answered {response.status_code}'

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    def test_an_empty_body_is_refused_not_crashed(self, client, event, name, path):
        response = client.post(path, data=b'', content_type='application/json')
        assert response.status_code < 500, f'{name} answered {response.status_code}'

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    def test_a_json_scalar_where_an_object_goes_is_refused(self, client, event, name, path):
        """`json.loads` happily returns a string or a number, and then every
        `payload.get(...)` in the view is an AttributeError."""
        for body in (b'"just a string"', b'42', b'[]', b'null'):
            response = client.post(path, data=body, content_type='application/json')
            assert response.status_code < 500, (
                f'{name} answered {response.status_code} to {body!r}'
            )


# --- two people doing it at once --------------------------------------
#
# This app is single-process by design, but it is not single-*threaded*: the
# CP540 reader has its own thread, the backup runner has another, and a venue
# has several browsers refreshing the live views on every incoming time. SQLite
# takes one writer at a time, so the question these tests ask is not "does it
# lock" — it will — but "does the app wait it out, or lose a time".
#
# transaction=True is what makes them real: the ordinary django_db fixture wraps
# each test in a transaction that other threads cannot see, so a concurrency
# test written on it is a test of nothing.

@pytest.mark.django_db(transaction=True)
class TestTwoWritersAtOnce:

    def test_no_signal_is_lost_when_several_arrive_together(self, tmp_path,
                                                             monkeypatch):
        """The one promise this subsystem makes: **a time is never lost.**

        The CP540 reader thread and a browser posting a manual time can land in
        the same instant, and SQLite takes one writer at a time. So a signal may
        legitimately end up in the recovery file instead of the table — what may
        not happen is that it ends up in neither, which is what
        `record_signal` raising would mean.

        Both halves are counted here, deliberately. Writing this test is what
        found the gap: `Competition.get_current()` and `TimingSettings.load()`
        were read *above* the guard, so a lock on either raised straight out and
        the time went nowhere at all.

        (The test database is in-memory with a shared cache, which serialises
        far harder than WAL on a real file — so this is a much crueller run than
        a venue's, which is the right way round for a test like this.)
        """
        import datetime
        import threading

        from apps.competitions.models import Competition, CompetitionType
        from apps.timing import ingest
        from apps.timing.models import TimingSignal

        recovery = tmp_path / 'timing_unrecorded.log'
        monkeypatch.setattr(ingest, 'UNRECORDED_LOG', recovery)

        ctype = CompetitionType.objects.create(name='Slalom')
        Competition.objects.create(competition_type=ctype, name='Race',
                                   date=datetime.date(2026, 5, 1), is_active=True)

        raised = []
        start = threading.Barrier(8)

        def fire(n):
            from django.db import connection
            try:
                start.wait(timeout=10)
                ingest.record_signal(running_number=n, port=1, is_manual=False,
                                     device_time=datetime.time(10, 0, n % 60))
            except Exception as err:          # noqa: BLE001 - reported, not raised
                raised.append(err)
            finally:
                connection.close()

        threads = [threading.Thread(target=fire, args=(n,)) for n in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert raised == [], f'record_signal raised instead of capturing: {raised}'
        stored = TimingSignal.objects.count()
        captured = len(recovery.read_text(encoding='utf-8').splitlines())             if recovery.exists() else 0
        assert stored + captured == 8, (
            f'{8 - stored - captured} signal(s) went nowhere: '
            f'{stored} stored, {captured} in the recovery file'
        )

    def test_two_writers_on_one_bib_leave_one_entry(self):
        """A bib is unique per competition. Two operators assigning the same
        number at the same moment must not produce two rows — the constraint is
        what decides it, not who got there first."""
        import datetime
        import threading

        from django.db import IntegrityError

        from apps.competitions.models import Competition, CompetitionType
        from apps.participants.models import EventEntry, Participant

        ctype = CompetitionType.objects.create(name='Slalom')
        competition = Competition.objects.create(
            competition_type=ctype, name='Race', date=datetime.date(2026, 5, 1),
            is_active=True)
        people = [
            Participant.objects.create(competition_type=ctype, first_name='A',
                                       last_name=str(n),
                                       date_of_birth=datetime.date(2000, 1, 1))
            for n in range(4)
        ]

        refused = []
        start = threading.Barrier(len(people))

        def claim(person):
            from django.db import connection
            try:
                start.wait(timeout=10)
                EventEntry.objects.create(competition=competition,
                                          participant=person, bib_number=7)
            except IntegrityError:
                refused.append(person)
            finally:
                connection.close()

        threads = [threading.Thread(target=claim, args=(p,)) for p in people]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert EventEntry.objects.filter(competition=competition,
                                         bib_number=7).count() == 1
        assert len(refused) == len(people) - 1

    def test_a_live_read_does_not_block_behind_a_writer(self):
        """WAL mode's whole point here: a browser refreshing the arrangement
        must not be waiting on the reader thread's insert. Both finish."""
        import datetime
        import threading

        from django.test import Client

        from apps.competitions.models import Competition, CompetitionType
        from apps.timing.ingest import record_signal
        from apps.timing.models import TimingSignal

        ctype = CompetitionType.objects.create(name='Slalom')
        Competition.objects.create(competition_type=ctype, name='Race',
                                   date=datetime.date(2026, 5, 1), is_active=True)
        from django.contrib.auth.models import User
        user = User.objects.create_superuser(username='race-desk', email='',
                                             password='pw')

        done = []

        def write():
            from django.db import connection
            try:
                for n in range(1, 21):
                    record_signal(running_number=n, port=1, is_manual=False,
                                  device_time=datetime.time(10, 0, n % 60))
                done.append('writer')
            finally:
                connection.close()

        def read():
            from django.db import connection
            try:
                browser = Client()
                browser.force_login(user)
                for _ in range(10):
                    assert browser.get('/timing/arrangement/').status_code == 200
                done.append('reader')
            finally:
                connection.close()

        threads = [threading.Thread(target=write), threading.Thread(target=read)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert sorted(done) == ['reader', 'writer'], f'a thread hung: {done}'
        assert TimingSignal.objects.count() == 20
