import datetime, pytest
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from audit_matrix_test import build, drive
pytestmark = pytest.mark.django_db

@pytest.mark.parametrize("n", [20, 50, 100])
def test_age_assignment_scaling(client, n):
    comp, cc = build(assignment="age", starters=n)
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("timing:auto-state"))
    print(f"\nAGE  starters={n:4d}  auto-state queries={len(ctx)}")

@pytest.mark.parametrize("n", [20, 50, 100])
def test_manual_assignment_scaling(client, n):
    comp, cc = build(assignment="manual", starters=n)
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("timing:auto-state"))
    print(f"\nMAN  starters={n:4d}  auto-state queries={len(ctx)}")

@pytest.mark.parametrize("classes", [1, 3, 6])
def test_export_all_scaling(client, classes):
    comp, cc = build(starters=30)
    for i, name in enumerate(["2","3","4","5","6"][:classes-1]):
        o = comp.classes.get(name=name); o.is_running=True; o.run_position=1; o.save()
    drive(comp, count=10)
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("results:export-all"))
    print(f"\nEXPORT classes={classes}  queries={len(ctx)}")

def test_auto_state_payload_size(client):
    comp, cc = build(starters=200)
    drive(comp, count=20)
    r = client.get(reverse("timing:auto-state"))
    print(f"\nPAYLOAD auto-state 200 starters = {len(r.content)/1024:.0f} KiB")
    r = client.get(reverse("timing:arrangement"))
    print(f"PAYLOAD arrangement = {len(r.content)/1024:.0f} KiB")
