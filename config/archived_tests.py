"""What an archived competition refuses.

Archiving has two halves (see apps/competitions/archiving.py): the settings the
event was run under are frozen onto it, and the event stops taking writes. The
first half is worth nothing without the second — a result computed from July's
rules over times somebody recorded in November is not July's result, it is a
third thing nobody asked for.

"The event stops taking writes" is a claim about *every door in the app*, and
the app has around forty of them. So these tests do not name endpoints, for the
same reason ``hostility_tests.py`` does not: a list somebody has to remember to
extend is a list that is one endpoint out of date the moment a feature lands.
They **discover** the app's POST endpoints from the URLconf and ask each one the
same question — refuse, or be on ``STILL_WRITABLE`` with a reason written down.

``STILL_WRITABLE`` is the interesting half. Some writes are not writes *to the
event*: the device settings and the operator's Lock switch belong to the
installation, releasing a marshal-post claim belongs to a phone, creating a
competition and importing one make new events, and the device's own signal door
never refuses a time at all — it captures it onto the ignore list, because losing
one is the single thing apps/timing/ingest.py promises cannot happen.
"""

import inspect
import json

import pytest
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from apps.competitions import archiving

# url name -> why this endpoint still takes a POST while the event is archived.
# A new entry here is a decision; a missing one is a test failure, which is the
# direction this has to fail in.
STILL_WRITABLE = {
    # Installation-wide, not this event's: which device is attached, and the red
    # operator Lock. Both outlive any one competition.
    'timing:settings': 'the device configuration belongs to the installation',
    'timing:input-lock': 'the operator Lock belongs to the installation',
    # The device door never refuses a time — it stores it ignored instead (see
    # ingest._context). A refusal here would lose it.
    'timing:signal': 'a time is captured, never turned away',
    # A phone letting go of a post writes a device lock, not the race. A marshal
    # that cannot release is a post held until the claim goes stale.
    'timing:marshal-release': 'releasing a claim is device state, not race data',
    # These make *new* events (or new disciplines) rather than writing this one.
    'competitions:add': 'creates a new competition',
    'competitions:type-add': 'creates a competition type',
    'transfer:import': 'an archive import creates its own competition',
    'transfer:review': 'the import wizard writes the competition it creates',
    'transfer:cancel': 'drops a staged upload',
    'transfer:export': 'a download',
    'transfer:backup': 'the backup destination belongs to the installation',
    # A POST that saves nothing: the settings page's PDF preview, rendered from
    # the form as it currently stands.
    'results:export-sample': 'renders a preview, saves nothing',
}

# Whole URL namespaces this sweep does not cover, and why.
#
# `admin` is the deliberate fallback surface for anything the app's own screens
# don't edit — superuser-only, and locking it would leave a signed-off event
# nobody could correct even by hand, which is a door with no key rather than a
# read-only event. `accounts` is who may sign in and what they may open: it
# outlives every competition, and an operator locked out of user administration
# because last year's event is selected would be a bug with no upside.
UNCOVERED_NAMESPACES = {'admin', 'accounts'}


def _writes(callback):
    """Whether this view has a POST handler of its own — the app's definition of
    a door a write comes through.

    Read off the view rather than from a list: a class-based view is asked for a
    ``post`` method (``view_class`` is what Django leaves on ``as_view()`` for
    exactly this), and a function view is asked whether its own source carries
    ``@require_POST``, which is how every mutating function endpoint here is
    written. A plain function view without it is a read — the live payload
    endpoints answer a POST as readily as a GET, and refusing those would blank
    the very screens an archived event exists to be looked at on.
    """
    view_class = getattr(callback, 'view_class', None)
    if view_class is not None:
        return hasattr(view_class, 'post')
    try:
        return 'require_POST' in inspect.getsource(callback)
    except (OSError, TypeError):
        return False


def _post_endpoints():
    """Every (name, path) in the project that writes and needs no URL arguments.

    Argument-free only: an endpoint with a pk in its route needs a plausible pk
    to reach its own body at all, and the ones this app has (select, delete,
    duplicate, archive, reopen) are all deliberately *about* an archived
    competition rather than writes into one.

    Two namespaces are left out whole — see UNCOVERED_NAMESPACES.
    """
    found = []

    def walk(resolver, prefix, app_name):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                walk(entry, prefix + str(entry.pattern), entry.app_name or app_name)
            elif isinstance(entry, URLPattern) and entry.name:
                route = prefix + str(entry.pattern)
                if '<' in route or app_name in UNCOVERED_NAMESPACES:
                    continue
                if not _writes(entry.callback):
                    continue
                name = f'{app_name}:{entry.name}' if app_name else entry.name
                found.append((name, '/' + route))

    walk(get_resolver(), '', '')
    return sorted(set(found))


ENDPOINTS = [
    (name, path) for name, path in _post_endpoints()
    if name not in STILL_WRITABLE
]


@pytest.fixture
def archived_event(db):
    """An archived competition, active, with a class running and one starter —
    the state a club is in the evening after the event."""
    import datetime

    from apps.competitions.models import Competition, CompetitionClass, CompetitionType
    from apps.participants.models import EventEntry, Participant
    from apps.timing.models import TimingSettings

    ctype = CompetitionType.objects.create(name='Slalom')
    competition = Competition.objects.create(
        competition_type=ctype, name='Signed off', date=datetime.date(2026, 5, 1),
        is_active=True)
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
    settings_row = TimingSettings.load()
    settings_row.device = TimingSettings.Device.SIMULATOR
    settings_row.save()
    archiving.archive(competition)
    return {'competition': competition, 'cclass': cclass, 'participant': person}


def _was_refused(response):
    """Whether a response is this app saying "that event is archived".

    Two shapes, because the app has two kinds of write: a JSON endpoint answers
    409 carrying ``archived``, and a page redirects with the reason in the
    message store. Anything else — a 200, a 400, a form re-rendered — means the
    view got past the guard, which is the failure being looked for.
    """
    if response.status_code == 409:
        try:
            return bool(json.loads(response.content or b'{}').get('archived'))
        except ValueError:
            return False
    if response.status_code in (302, 303):
        messages = [str(m) for m in response.wsgi_request._messages]
        return any(str(archiving.READ_ONLY) == text for text in messages)
    return False


@pytest.mark.django_db
class TestAnArchivedEventTakesNoWrites:

    def test_there_are_endpoints_to_test(self):
        """A broken discovery makes every test below pass by testing nothing,
        which is the one way a sweep like this rots unnoticed."""
        assert len(ENDPOINTS) >= 15, ENDPOINTS

    def test_every_named_exception_still_exists(self):
        """``STILL_WRITABLE`` grants a pass; a stale entry grants it to nothing
        and hides that its endpoint was renamed away from the guard."""
        real = {name for name, _ in _post_endpoints()}
        missing = sorted(set(STILL_WRITABLE) - real)
        assert not missing, f'STILL_WRITABLE names endpoints that no longer exist: {missing}'

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    def test_a_post_is_refused(self, client, archived_event, name, path):
        response = client.post(path, data=json.dumps({}),
                               content_type='application/json')
        if response.status_code == 405:
            return  # a GET-only route; nothing to refuse
        assert _was_refused(response), (
            f'{name} answered {response.status_code} instead of refusing an '
            f'archived competition — guard it (apps/competitions/archiving.py) '
            f'or add it to STILL_WRITABLE with the reason'
        )

    @pytest.mark.parametrize('name, path', ENDPOINTS, ids=[n for n, _ in ENDPOINTS])
    def test_a_form_post_is_refused(self, client, archived_event, name, path):
        """The same sweep as a form post rather than JSON: several of these are
        pages, and a page that only refuses ``application/json`` refuses
        nothing an operator can actually send."""
        response = client.post(path, data={})
        if response.status_code == 405:
            return
        assert _was_refused(response), (
            f'{name} answered {response.status_code} to a form post instead of '
            f'refusing an archived competition'
        )
