from django.db import connection
from django.http import JsonResponse


def health(request):
    try:
        connection.cursor().execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "db_unavailable"}, status=503)
    return JsonResponse({"status": "ok"}, status=200)
