from django import template

register = template.Library()


@register.filter
def participant_classes(participant, competition):
    """The class(es) a participant belongs to under the competition's assignment
    method — a list of CompetitionClass (may repeat for manual multi-entry)."""
    if not competition:
        return []
    return competition.classes_for_participant(participant)
