"""HTTP routes for the videos blueprint.

Thin glue over :class:`teslausb_web.services.video_service.VideoService`.
No business logic — every operation that touches the filesystem is
delegated to the service so the route handlers stay short enough to
read top-to-bottom.

B-1 adaptation notes:

* **No IMG gate.** v1 had a ``before_request`` hook that checked
  ``IMG_CAM_PATH`` existed. B-1 has no IMG files; the service
  handles a missing TeslaCam directory by returning empty results.
* **No mode-token gate on delete.** v1 only permitted deletion in
  edit-mode. In B-1 the partitions are always rw — the Delete
  button is rendered unconditionally and the route does the
  containment check on every call.
* **Browser GET → 302 to mapping.** Mirrors v1: ``GET /videos/``
  is consumed as an XHR by the right-rail video panel on the
  mapping page; a non-XHR GET means the user typed the URL, so we
  redirect them to the page that hosts the panel.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from flask import (
    Blueprint,
    Response,
    abort,
    after_this_request,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from teslausb_web.services.video_service import (
    DeletionError,
    EventSummary,
    PathSecurityError,
    RangeParseError,
    SessionGroup,
    VideoService,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

videos_bp = Blueprint("videos", __name__, url_prefix="/videos")

_DEFAULT_PER_PAGE = 12
_HTTP_RANGE_NOT_SATISFIABLE = 416


def _get_service() -> VideoService:
    svc = current_app.extensions.get("video_service")
    if not isinstance(svc, VideoService):
        raise RuntimeError("video_service extension is not configured")
    return svc


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _serialize_event_summary(event: EventSummary) -> dict[str, object]:
    out: dict[str, object] = {
        "name": event.name,
        "datetime": event.datetime_str,
        "size_mb": event.size_mb,
        "camera_videos": {k: v for k, v in event.camera_videos.to_dict().items() if v},
    }
    if event.city:
        out["city"] = event.city
    if event.reason:
        out["reason"] = event.reason
    return out


def _serialize_session(session: SessionGroup) -> dict[str, object]:
    out: dict[str, object] = {
        "name": session.name,
        "datetime": session.datetime_str,
        "size_mb": session.size_mb,
        "camera_videos": {k: v for k, v in session.camera_videos.to_dict().items() if v},
    }
    encrypted = {k: v for k, v in session.encrypted_videos.to_dict().items() if v}
    if encrypted:
        out["encrypted_videos"] = encrypted
    return out


@videos_bp.route("/", endpoint="file_browser")
def file_browser() -> ResponseReturnValue:
    """List clips for the mapping panel (XHR) or redirect (browser).

    The v1 contract is unchanged: ``X-Requested-With:
    XMLHttpRequest`` returns the JSON payload the right-rail panel
    consumes; a plain browser GET 302s to the mapping page.
    """
    if not _wants_json():
        return redirect(url_for("mapping.map_view"))

    svc = _get_service()
    folders = svc.list_folders()
    current_folder = request.args.get("folder") or (folders[0].name if folders else None)
    try:
        page_num = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page_num = 1
    per_page = _DEFAULT_PER_PAGE

    if not current_folder:
        return jsonify({"events": [], "has_next": False, "folder_structure": "events"})

    structure = svc.get_folder_structure(current_folder)
    if structure == "flat":
        sessions, total = svc.group_videos_by_session(
            current_folder, page=page_num, per_page=per_page
        )
        items = [_serialize_session(s) for s in sessions]
    else:
        events, total = svc.get_events(current_folder, page=page_num, per_page=per_page)
        items = [_serialize_event_summary(e) for e in events]

    total_videos = svc.count_videos_in_folder(current_folder)
    return jsonify(
        {
            "events": items,
            "has_next": (page_num * per_page) < total,
            "next_page": page_num + 1,
            "total_count": total,
            "total_video_count": total_videos,
            "folder_structure": structure,
        }
    )


@videos_bp.route("/stream/<path:filepath>", endpoint="stream_video")
def stream_video(filepath: str) -> ResponseReturnValue:
    """Serve a clip with HTTP Range support (206 / 200)."""
    svc = _get_service()
    try:
        resolved = svc.resolve_clip_path(filepath)
    except (PathSecurityError, FileNotFoundError):
        abort(404)

    file_size = resolved.path.stat().st_size
    range_header = request.headers.get("Range")
    try:
        rng = svc.parse_range(range_header, file_size)
    except RangeParseError:
        return Response(status=_HTTP_RANGE_NOT_SATISFIABLE)

    if rng is None:
        response = send_file(resolved.path, mimetype="video/mp4")
        response.headers["Accept-Ranges"] = "bytes"
        return response

    resp = Response(
        svc.stream_iter(resolved.path, rng.start, rng.end),
        status=206,
        mimetype="video/mp4",
        direct_passthrough=True,
    )
    resp.headers["Content-Range"] = f"bytes {rng.start}-{rng.end}/{rng.full_size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(rng.length)
    if request.method == "HEAD":
        resp.response = []
    return resp


@videos_bp.route("/sei/<path:filepath>", endpoint="fetch_video_for_sei")
def fetch_video_for_sei(filepath: str) -> ResponseReturnValue:
    """Serve the full clip body for client-side SEI parsing.

    Unlike ``/stream/``, this endpoint deliberately does NOT
    advertise Range support — the JS-side parser needs the whole
    file in one shot and Flask's conditional-GET would break that.
    """
    svc = _get_service()
    try:
        resolved = svc.resolve_clip_path(filepath)
    except (PathSecurityError, FileNotFoundError):
        abort(404)
    response = send_file(
        resolved.path,
        mimetype="video/mp4",
        as_attachment=False,
        conditional=False,
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@videos_bp.route("/download/<path:filepath>", endpoint="download_video")
def download_video(filepath: str) -> ResponseReturnValue:
    """Download a single clip as an attachment."""
    svc = _get_service()
    try:
        resolved = svc.resolve_clip_path(filepath)
    except (PathSecurityError, FileNotFoundError):
        abort(404)
    return send_file(
        resolved.path,
        as_attachment=True,
        download_name=resolved.path.name,
    )


@videos_bp.route("/download_event/<folder>/<event_name>", endpoint="download_event")
def download_event(folder: str, event_name: str) -> ResponseReturnValue:
    """Stream a ZIP of every clip in an event."""
    svc = _get_service()
    try:
        zip_path, filename = svc.download_event_zip(folder, event_name)
    except FileNotFoundError:
        abort(404)
    except (OSError, PathSecurityError) as exc:
        logger.warning("download_event: %s/%s: %s", folder, event_name, exc)
        abort(404)

    @after_this_request
    def _cleanup(response: Response) -> Response:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("download_event: unlink %s failed: %s", zip_path, exc)
        return response

    return send_file(
        zip_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip",
    )


@videos_bp.route(
    "/delete_event/<folder>/<event_name>",
    methods=["POST"],
    endpoint="delete_event",
)
def delete_event(folder: str, event_name: str) -> ResponseReturnValue:
    """Delete an event or session's clips.

    No mode-token gate — the v1 ``edit``-mode requirement is dropped
    in B-1. Path containment alone is the security boundary.
    """
    svc = _get_service()
    try:
        outcome = svc.safe_delete_clip(folder, event_name)
    except FileNotFoundError:
        abort(404)
    except PathSecurityError as exc:
        logger.warning("delete_event: %s/%s blocked: %s", folder, event_name, exc)
        abort(404)
    except DeletionError as exc:
        logger.error("delete_event: %s/%s failed: %s", folder, event_name, exc)
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify(
        {
            "success": True,
            "deleted_count": outcome.deleted_count,
            "deleted_files": list(outcome.deleted_files),
            "error_count": outcome.error_count,
        }
    )


@videos_bp.route("/event/<folder>/<event_name>", endpoint="event_player")
def event_player(folder: str, event_name: str) -> ResponseReturnValue:
    """Render the event-player template for one event."""
    svc = _get_service()
    sanitized_folder = Path(folder).name
    sanitized_event = Path(event_name).name
    structure = svc.get_folder_structure(sanitized_folder)
    details = svc.get_event_details(sanitized_folder, sanitized_event)
    if details is None and structure != "flat":
        abort(404)
    cloud_connected = _cloud_provider_connected()
    return render_template(
        "event_player.html",
        folder=sanitized_folder,
        event=_serialise_event_for_template(details),
        folder_structure=structure,
        cloud_provider_connected=cloud_connected,
    )


def _cloud_provider_connected() -> bool:
    """Best-effort probe of the cloud provider state.

    ``CloudOAuthService.load_credentials()`` returns ``None`` when no
    provider has been authorised — that's the closest analogue to
    v1's ``cloud_provider_connected`` flag. Errors degrade to
    ``False`` so the template never sees an exception.
    """
    svc = current_app.extensions.get("cloud_oauth_service")
    if svc is None:
        return False
    loader = getattr(svc, "load_credentials", None)
    if loader is None:
        return False
    try:
        return loader() is not None
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("_cloud_provider_connected: probe failed: %s", exc)
        return False


def _serialise_event_for_template(details: object) -> dict[str, object] | None:
    """Convert :class:`EventDetails` (frozen dataclass) into the dict the
    Jinja template expects.

    Returns ``None`` when there is no event (flat-folder sessions go
    through the same template with a different folder_structure
    branch; the template short-circuits when ``event`` is None).
    """
    if details is None:
        return None
    # Local import keeps the runtime cost of the type out of the
    # request hot-path when ``details`` is ``None``.
    from teslausb_web.services.video_service import EventDetails  # noqa: PLC0415

    d = cast("EventDetails", details)
    return {
        "name": d.name,
        "path": d.path,
        "timestamp": d.timestamp,
        "datetime": d.datetime_str,
        "size_mb": d.size_mb,
        "camera_videos": d.camera_videos.to_dict(),
        "encrypted_videos": d.encrypted_videos.to_dict(),
        "city": d.city,
        "reason": d.reason,
        "metadata": d.metadata,
        "clips": [
            {
                "timestamp_str": c.timestamp_str,
                "timestamp": c.timestamp,
                "camera_videos": c.camera_videos.to_dict(),
                "encrypted_videos": c.encrypted_videos.to_dict(),
            }
            for c in d.clips
        ],
        "starting_clip_index": d.starting_clip_index,
    }
