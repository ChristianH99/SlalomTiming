import datetime, json, collections
import pytest
from django.urls import reverse
from apps.competitions.models import MarshalPost
from audit_perf_test import build, drive
pytestmark = pytest.mark.django_db


def _sizes(obj):
    """Bytes each top-level item key contributes across the whole payload."""
    per_key = collections.Counter()
    for item in obj["items"]:
        for k, v in item.items():
            per_key[k] += len(json.dumps(v, separators=(",", ":")))
    return per_key


@pytest.mark.parametrize("posts,tasks", [(0, ""), (4, "1-6")])
def test_where_the_bytes_are(client, posts, tasks):
    comp, _ = build(200)
    comp.penalties_by_marshal_posts = True
    comp.save()
    for n in range(1, posts + 1):
        MarshalPost.objects.create(competition=comp, number=n, tasks=tasks,
                                   handles_stop_line=(n == 1))
    drive(comp, 40)
    raw = client.get(reverse("timing:auto-state")).content
    data = json.loads(raw)
    per_key = _sizes(data)
    total = sum(per_key.values())
    print(f"\n=== {posts} posts: payload {len(raw)/1024:.1f} KiB, items {total/1024:.1f} KiB "
          f"({len(data['items'])} items)")
    for k, n in per_key.most_common(8):
        print(f"     {k:16} {n/1024:8.1f} KiB  ({100*n/total:4.1f}%)")
