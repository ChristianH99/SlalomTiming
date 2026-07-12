from django.db import models


class CompetitionType(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Competition(models.Model):
    competition_type = models.ForeignKey(
        CompetitionType, on_delete=models.PROTECT, related_name="competitions"
    )
    name = models.CharField(max_length=150)
    date = models.DateField()
    is_active = models.BooleanField(default=False, help_text="The competition currently being run.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.name} ({self.date:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            CompetitionClass.objects.bulk_create(
                CompetitionClass(competition=self, code=code)
                for code, _ in CompetitionClass.Code.choices
            )

    def active_classes(self):
        return [cc.code for cc in self.classes.filter(is_running=True).order_by("code")]

    def class_for_birth_year(self, birth_year):
        if birth_year is None:
            return None
        age = self.date.year - birth_year
        for competition_class in self.classes.filter(is_running=True):
            if (
                competition_class.age_from is not None
                and competition_class.age_to is not None
                and competition_class.age_from <= age <= competition_class.age_to
            ):
                return competition_class
        return None

    @classmethod
    def get_current(cls):
        return cls.objects.filter(is_active=True).first()


class CompetitionClass(models.Model):
    class Code(models.TextChoices):
        ONE = "1", "Class 1"
        TWO = "2", "Class 2"
        THREE = "3", "Class 3"
        FOUR = "4", "Class 4"
        FIVE = "5", "Class 5"
        SIX = "6", "Class 6"
        E = "E", "Class E"

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="classes")
    code = models.CharField(max_length=1, choices=Code.choices)
    is_running = models.BooleanField(default=False)
    age_from = models.PositiveIntegerField(null=True, blank=True, help_text="Starting age, e.g. 6")
    age_to = models.PositiveIntegerField(null=True, blank=True, help_text="Ending age, e.g. 7")

    class Meta:
        ordering = ["code"]
        unique_together = ("competition", "code")

    def __str__(self):
        return f"{self.competition} – {self.get_code_display()}"

    def birth_year_range(self):
        """Birth years spanned by this class's age range, oldest (from age_to)
        first. Deliberately not reordered by magnitude: age_to always drives
        the first value and age_from always drives the second, so changing
        just one age field only ever moves the number it corresponds to."""
        if self.age_from is None or self.age_to is None:
            return None
        year = self.competition.date.year
        return (year - self.age_to, year - self.age_from)
