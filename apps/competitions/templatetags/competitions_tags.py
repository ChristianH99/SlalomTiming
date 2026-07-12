from django import template

register = template.Library()


@register.filter
def class_for_birth_year(birth_year, competition):
    if not competition or not birth_year:
        return None
    competition_class = competition.class_for_birth_year(birth_year)
    return competition_class.get_code_display() if competition_class else None
