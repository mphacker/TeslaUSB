const CAMERA_ANGLES = ["front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"];
const CAMERA_LABELS = {
    front: "Front",
    back: "Rear",
    left_repeater: "Left repeater",
    right_repeater: "Right repeater",
    left_pillar: "Left pillar",
    right_pillar: "Right pillar",
};
const CAMERA_ICONS = {
    front: "camera",
    back: "rotate-ccw-square",
    left_repeater: "arrow-left",
    right_repeater: "arrow-right",
    left_pillar: "arrow-down-left",
    right_pillar: "arrow-down-right",
};

function escapeHtml(text) {
    return String(text || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function basePath(videoPath) {
    const angle = CAMERA_ANGLES.find((candidate) => String(videoPath).includes(`-${candidate}.mp4`));
    return angle ? String(videoPath).replace(`-${angle}.mp4`, "") : String(videoPath).replace(/\.mp4$/u, "");
}

function streamUrl(template, videoPath) {
    return template.replace("__PATH__", encodeURIComponent(videoPath));
}

function clipTimeLabel(timestamp) {
    if (!timestamp) {
        return "Unknown time";
    }
    return new Date(String(timestamp).replace(" ", "T")).toLocaleString();
}

export function createSentryInspector(options) {
    const sentryList = options.sentryList;
    const sentryEmpty = options.sentryEmpty;
    const playerCard = options.playerCard;
    const playerTitle = options.playerTitle;
    const playerSummary = options.playerSummary;
    const playerVideo = options.playerVideo;
    const cameraRow = options.cameraRow;
    const prevClipButton = options.prevClipButton;
    const nextClipButton = options.nextClipButton;
    const hud = options.hud;
    let sentryEvents = [];
    let overlayWaypoints = [];
    let overlayTelemetryById = new Map();
    let parsedSeiMessages = [];
    let clipList = [];
    let clipIndex = 0;
    let currentAngle = "front";
    let currentBasePath = "";
    let streamTemplate = options.streamTemplate;

    function currentClipPath() {
        const clip = clipList[clipIndex];
        return clip ? `${clip.basePath}-${currentAngle}.mp4` : "";
    }

    function renderHud(row, source) {
        hud.coords.textContent = row && Number.isFinite(row.lat) ? `${row.lat.toFixed(4)}, ${row.lon.toFixed(4)}` : "—";
        hud.speed.textContent = row && Number.isFinite(row.speed_mps) ? `${Math.round(Math.abs(row.speed_mps) * 2.23694)} mph` : "—";
        const gear = row?.gear || row?.gear_state || "—";
        hud.gear.textContent = String(gear).replace("GEAR_", "").replaceAll("_", " ");
        const autopilot = row?.autopilot_state || "NONE";
        hud.autopilot.textContent = String(autopilot).replaceAll("_", " ");
        const steering = row?.steering_angle ?? row?.steering_wheel_angle;
        hud.steering.textContent = steering == null ? "—" : `${Math.round(Number(steering))}°`;
        hud.brake.textContent = row?.brake_applied ? "Applied" : "Off";
        const blinkers = [row?.blinker_on_left ? "Left" : "", row?.blinker_on_right ? "Right" : ""]
            .filter(Boolean)
            .join(" + ");
        hud.blinkers.textContent = blinkers || "Off";
        hud.source.textContent = source;
    }

    function bestWaypointForCurrentFrame() {
        const currentFrame = Math.round((playerVideo.currentTime || 0) * 36);
        const base = currentBasePath;
        const candidates = overlayWaypoints.filter((waypoint) => basePath(waypoint.video_path || "") === base);
        let best = null;
        let bestDelta = Number.POSITIVE_INFINITY;
        candidates.forEach((waypoint) => {
            const delta = Math.abs(Number(waypoint.frame_offset || 0) - currentFrame);
            if (delta < bestDelta) {
                best = waypoint;
                bestDelta = delta;
            }
        });
        return best;
    }

    function bestSeiForCurrentFrame() {
        const currentFrame = Math.round((playerVideo.currentTime || 0) * 36);
        let best = null;
        let bestDelta = Number.POSITIVE_INFINITY;
        parsedSeiMessages.forEach((message) => {
            const frame = Number(message.frame_seq_no || 0);
            const delta = Math.abs(frame - currentFrame);
            if (delta < bestDelta) {
                best = message;
                bestDelta = delta;
            }
        });
        return best;
    }

    function refreshHud() {
        const sei = bestSeiForCurrentFrame();
        if (sei) {
            renderHud(
                {
                    lat: Number(sei.latitude_deg),
                    lon: Number(sei.longitude_deg),
                    speed_mps: Number(sei.vehicle_speed_mps),
                    gear_state: sei.gear_state,
                    autopilot_state: sei.autopilot_state,
                    steering_wheel_angle: Number(sei.steering_wheel_angle),
                    brake_applied: Boolean(sei.brake_applied),
                    blinker_on_left: Boolean(sei.blinker_on_left),
                    blinker_on_right: Boolean(sei.blinker_on_right),
                },
                "Dashcam SEI"
            );
            return;
        }
        const waypoint = bestWaypointForCurrentFrame();
        if (waypoint) {
            renderHud({ ...waypoint, ...overlayTelemetryById.get(String(waypoint.id)) }, "Indexed trip telemetry");
            return;
        }
        renderHud(null, "Unavailable");
    }

    async function maybeParseDashcam(videoPath) {
        parsedSeiMessages = [];
        if (!window.DashcamMP4 || !window.DashcamHelpers || !window.protobuf || !streamTemplate) {
            return;
        }
        try {
            const proto = await window.DashcamHelpers.initProtobuf(options.dashcamProtoUrl);
            const response = await fetch(streamUrl(streamTemplate, videoPath));
            if (!response.ok) {
                return;
            }
            const parser = new window.DashcamMP4(await response.arrayBuffer());
            parsedSeiMessages = parser.extractSeiMessages(proto.SeiMetadata) || [];
        } catch {
            parsedSeiMessages = [];
        }
    }

    function cameraButton(angle) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mapping-camera-button";
        if (angle === currentAngle) {
            button.classList.add("is-active");
        }
        button.innerHTML = `<svg aria-hidden="true"><use href="${options.spriteUrl}#icon-${CAMERA_ICONS[angle]}"></use></svg><span>${CAMERA_LABELS[angle]}</span>`;
        button.addEventListener("click", () => switchCamera(angle));
        return button;
    }

    function renderCameraButtons() {
        cameraRow.replaceChildren();
        CAMERA_ANGLES.forEach((angle) => cameraRow.append(cameraButton(angle)));
    }

    async function loadVideo(videoPath) {
        const url = streamTemplate ? streamUrl(streamTemplate, videoPath) : "";
        if (!url) {
            playerSummary.textContent = "Clip streaming is not available in this B-1 deploy.";
            playerVideo.removeAttribute("src");
            playerVideo.load();
            return;
        }
        playerVideo.src = url;
        playerVideo.load();
        await maybeParseDashcam(videoPath);
        refreshHud();
    }

    async function switchCamera(angle) {
        currentAngle = angle;
        renderCameraButtons();
        const path = currentClipPath();
        if (path) {
            await loadVideo(path);
        }
    }

    async function selectClip(index) {
        clipIndex = Math.max(0, Math.min(index, clipList.length - 1));
        const clip = clipList[clipIndex];
        if (!clip) {
            return;
        }
        currentBasePath = clip.basePath;
        playerTitle.textContent = clip.title;
        playerSummary.textContent = `${clip.summary} · ${clipTimeLabel(clip.timestamp)}`;
        renderCameraButtons();
        await loadVideo(currentClipPath());
    }

    async function openClip(context) {
        overlayWaypoints = Array.isArray(context.waypoints) ? context.waypoints : [];
        overlayTelemetryById = new Map(
            Object.entries(context.telemetry || {}).map(([key, value]) => [String(key), value])
        );
        clipList = Array.from(
            new Map(
                overlayWaypoints
                    .filter((waypoint) => waypoint.video_path)
                    .map((waypoint) => [basePath(waypoint.video_path), {
                        basePath: basePath(waypoint.video_path),
                        timestamp: waypoint.timestamp,
                        title: waypoint.video_path.split("/").pop() || waypoint.video_path,
                        summary: context.summary || "Route clip",
                    }])
            ).values()
        );
        if (clipList.length === 0 && context.videoPath) {
            clipList = [{
                basePath: basePath(context.videoPath),
                timestamp: context.timestamp,
                title: context.videoPath.split("/").pop() || context.videoPath,
                summary: context.summary || "Event clip",
            }];
        }
        currentAngle = CAMERA_ANGLES.find((angle) => String(context.videoPath || "").includes(`-${angle}.mp4`)) || "front";
        playerCard.hidden = false;
        await selectClip(
            Math.max(
                0,
                clipList.findIndex((clip) => clip.basePath === basePath(context.videoPath || ""))
            )
        );
    }

    async function openEvent(event) {
        const summary = `${(event.event_type || "event").replaceAll("_", " ")} · ${event.severity || "unknown severity"}`;
        let waypoints = [];
        let telemetry = {};
        let videoPath = event.video_path || "";
        if (event.trip_id) {
            const [route, cold] = await Promise.all([
                options.loadTripRoute(event.trip_id),
                options.loadTripTelemetry(event.trip_id),
            ]);
            waypoints = route.properties?.waypoints || [];
            telemetry = cold.telemetry || {};
        } else if (videoPath) {
            const payload = await options.fetchJson(`${options.api.waypoints_for_clip}?path=${encodeURIComponent(videoPath)}`);
            waypoints = payload.waypoints || [];
            if (payload.trip_id) {
                const cold = await options.loadTripTelemetry(payload.trip_id);
                telemetry = cold.telemetry || {};
            }
        }
        if (!videoPath && event.source_folder && event.event_folder) {
            const listing = await options.fetchJson(
                options.api.event_clips_template
                    .replace("__FOLDER__", encodeURIComponent(event.source_folder))
                    .replace("__EVENT__", encodeURIComponent(event.event_folder))
            );
            videoPath = listing.first_front ? `${listing.folder}/${listing.first_front}` : (listing.front_clips || [""])[0];
        }
        await openClip({
            summary,
            timestamp: event.timestamp,
            videoPath,
            waypoints,
            telemetry,
        });
    }

    function renderSentryEvents(events) {
        sentryEvents = events;
        sentryList.replaceChildren();
        sentryEmpty.hidden = events.length > 0;
        events.forEach((event) => {
            const item = document.createElement("li");
            item.className = "mapping-sentry-item";
            const folderInfo = [event.source_folder, event.event_folder].filter(Boolean).join(" / ");
            item.innerHTML = `
                <div class="mapping-sentry-item-head">
                    <div>
                        <p class="mapping-sentry-item-title">${escapeHtml((event.event_type || "event").replaceAll("_", " "))}</p>
                        <p class="mapping-sentry-item-meta">${escapeHtml(clipTimeLabel(event.timestamp))}</p>
                    </div>
                    <span class="mapping-summary-chip">${escapeHtml(event.severity || "unknown")}</span>
                </div>
                <p class="mapping-sentry-copy">${escapeHtml(event.description || folderInfo || "No description")}</p>
                <div class="mapping-sentry-actions">
                    <button class="mapping-sentry-link" type="button">Open inspector</button>
                </div>
            `;
            item.querySelector("button")?.addEventListener("click", () => {
                options.onSelectEvent(event);
            });
            sentryList.append(item);
        });
    }

    playerVideo.addEventListener("timeupdate", refreshHud);
    prevClipButton.addEventListener("click", () => {
        if (clipList.length > 0) {
            selectClip(clipIndex - 1).catch(() => {});
        }
    });
    nextClipButton.addEventListener("click", () => {
        if (clipList.length > 0) {
            selectClip(clipIndex + 1).catch(() => {});
        }
    });

    return {
        renderSentryEvents,
        async openTripClip(payload) {
            await openClip(payload);
        },
        async openEvent(event) {
            await openEvent(event);
        },
        setStreamTemplate(template) {
            streamTemplate = template;
        },
    };
}
