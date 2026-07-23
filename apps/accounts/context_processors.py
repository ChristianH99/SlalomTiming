from . import pages


def access(request):
    """The current user's allowed page keys (so the sidebar shows only permitted
    items) plus a superuser flag (gates the User Access link)."""
    user = getattr(request, "user", None)
    if user is None:
        return {"nav_pages": set(), "can_manage_users": False}
    return {
        "nav_pages": pages.user_pages(user),
        "can_manage_users": user.is_superuser,
    }
