from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme

from apps.core.security import (
    authenticate_shared_password,
    is_shared_access_login_throttled,
    record_shared_access_login_failure,
)


def health(request):
    try:
        connection.cursor().execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "db_unavailable"}, status=503)
    return JsonResponse({"status": "ok"}, status=200)


def shared_access_view(request: HttpRequest) -> HttpResponse:
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = "/"

    error = ""
    if request.method == "POST":
        if is_shared_access_login_throttled(request):
            return HttpResponse("Too many failed shared-password attempts.", status=429)
        password = request.POST.get("password", "")
        if authenticate_shared_password(request, password):
            response = HttpResponse(status=303)
            response["Location"] = next_url
            return response
        record_shared_access_login_failure(request)
        error = "Invalid shared password."

    return render(
        request,
        "core/shared_access.html",
        {"next": next_url, "error": error},
    )
