function formatDateLabel(dateText) {
    if (!dateText) {
        return "Unknown day";
    }
    const [year, month, day] = dateText.split("-").map((part) => Number.parseInt(part, 10));
    const date = new Date(year, (month || 1) - 1, day || 1);
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function formatDistance(distanceKm) {
    return `${(Number(distanceKm || 0) * 0.621371).toFixed(1)} mi`;
}

function formatDuration(durationSeconds) {
    const minutes = Math.max(0, Math.round(Number(durationSeconds || 0) / 60));
    return `${minutes} min`;
}

function createChartRows(container, rows, getColor) {
    container.replaceChildren();
    const maxValue = rows.reduce((largest, row) => Math.max(largest, Number(row.value || 0)), 0) || 1;
    rows.forEach((row) => {
        const wrapper = document.createElement("div");
        wrapper.className = "mapping-chart-row";
        const label = document.createElement("span");
        label.className = "mapping-chart-label";
        label.textContent = row.label;
        const shell = document.createElement("span");
        shell.className = "mapping-chart-bar-shell";
        const bar = document.createElement("span");
        bar.className = "mapping-chart-bar";
        bar.style.setProperty("--bar-value", `${(Number(row.value || 0) / maxValue) * 100}%`);
        bar.style.setProperty("--bar-color", getColor(row));
        const value = document.createElement("span");
        value.className = "mapping-chart-value";
        value.textContent = String(row.value || 0);
        shell.append(bar);
        wrapper.append(label, shell, value);
        container.append(wrapper);
    });
}

export function createEventsPanel(options) {
    const dayList = options.dayList;
    const tripList = options.tripList;
    const filterList = options.filterList;
    const dayCountChip = options.dayCountChip;
    const tripCountChip = options.tripCountChip;
    const daysEmpty = options.daysEmpty;
    const tripsEmpty = options.tripsEmpty;
    const filtersEmpty = options.filtersEmpty;
    const returnButton = options.returnButton;
    const statsGrid = options.statsGrid;
    const typeChart = options.typeChart;
    const severityChart = options.severityChart;
    const timelineChart = options.timelineChart;
    const fsdChart = options.fsdChart;
    const spriteUrl = options.spriteUrl;

    function dayButton(day, isActive) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mapping-day-button";
        if (isActive) {
            button.classList.add("is-active");
        }
        button.innerHTML = `
            <svg aria-hidden="true"><use href="${spriteUrl}#icon-calendar-days"></use></svg>
            <span>
                <span class="mapping-day-title">${formatDateLabel(day.date)}</span>
                <span class="mapping-day-meta">${day.trip_count} trips · ${formatDistance(day.total_distance_km)} · ${day.event_count} events</span>
            </span>
        `;
        button.addEventListener("click", () => options.onSelectDay(day.date));
        return button;
    }

    function tripButton(trip, isActive) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mapping-trip-button";
        if (isActive) {
            button.classList.add("is-active");
        }
        const label = trip.start_time ? new Date(String(trip.start_time).replace(" ", "T")).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : `Trip ${trip.trip_id || trip.id}`;
        button.innerHTML = `
            <svg aria-hidden="true"><use href="${spriteUrl}#icon-navigation"></use></svg>
            <span>
                <span class="mapping-trip-title">${label}</span>
                <span class="mapping-trip-meta">${formatDistance(trip.distance_km)} · ${formatDuration(trip.duration_seconds)} · ${trip.source_folder || "Unknown"}</span>
            </span>
        `;
        button.addEventListener("click", () => options.onSelectTrip(trip));
        return button;
    }

    function filterButton(eventType, count, enabled, iconName, accent) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mapping-filter-chip";
        button.style.setProperty("--pill-accent", accent);
        if (enabled) {
            button.classList.add("is-active");
        }
        button.innerHTML = `
            <svg aria-hidden="true"><use href="${spriteUrl}#icon-${iconName}"></use></svg>
            <span>${eventType.replace(/_/g, " ")}</span>
            <span>${count}</span>
        `;
        button.addEventListener("click", () => options.onToggleEventType(eventType));
        return button;
    }

    function statCard(label, value) {
        const article = document.createElement("article");
        article.className = "mapping-stat-card";
        article.innerHTML = `<p class="mapping-stat-label">${label}</p><p class="mapping-stat-number">${value}</p>`;
        return article;
    }

    returnButton.addEventListener("click", () => options.onReturnToLatestDay());

    return {
        renderDays(days, currentDate, showingAllRoutes) {
            dayList.replaceChildren();
            dayCountChip.textContent = `${days.length} days`;
            daysEmpty.hidden = days.length > 0;
            returnButton.hidden = !showingAllRoutes;
            days.forEach((day) => {
                const item = document.createElement("li");
                item.append(dayButton(day, !showingAllRoutes && day.date === currentDate));
                dayList.append(item);
            });
        },
        renderTrips(trips, currentTripId) {
            tripList.replaceChildren();
            tripCountChip.textContent = `${trips.length} trips`;
            tripsEmpty.hidden = trips.length > 0;
            trips.forEach((trip) => {
                const item = document.createElement("li");
                item.append(tripButton(trip, (trip.trip_id || trip.id) === currentTripId));
                tripList.append(item);
            });
        },
        renderFilters(summaryRows) {
            filterList.replaceChildren();
            filtersEmpty.hidden = summaryRows.length > 0;
            summaryRows.forEach((row) => {
                filterList.append(filterButton(row.eventType, row.count, row.enabled, row.iconName, row.accent));
            });
        },
        renderStats(stats, drivingStats, charts) {
            statsGrid.replaceChildren();
            const cards = [
                ["Trips", stats.trip_count || 0],
                ["Events", stats.event_count || 0],
                ["Files indexed", stats.indexed_file_count || 0],
                ["Mapped clips", stats.mapped_file_count || 0],
                ["Distance", formatDistance(drivingStats.total_distance_km || stats.total_distance_km || 0)],
                ["FSD usage", `${Math.round(Number(drivingStats.fsd_usage_pct || 0))}%`],
                ["Avg speed", `${Math.round(Number(drivingStats.avg_speed_mph || 0))} mph`],
                ["Warnings", drivingStats.warning_events || 0],
            ];
            cards.forEach(([label, value]) => {
                statsGrid.append(statCard(label, value));
            });
            createChartRows(
                typeChart,
                (charts.by_type || []).map((row) => ({ label: row.label, value: row.value })),
                () => "var(--btn-primary-bg)"
            );
            createChartRows(
                severityChart,
                (charts.by_severity || []).map((row) => ({ label: row.severity, value: row.value, color: row.color })),
                (row) => row.color || "var(--msg-warning-text)"
            );
            createChartRows(
                timelineChart,
                (charts.over_time || []).slice(-7).map((row) => ({ label: row.day.slice(5), value: row.value })),
                () => "var(--btn-info-bg)"
            );
            createChartRows(
                fsdChart,
                (charts.fsd_timeline || []).slice(-7).map((row) => ({ label: row.day.slice(5), value: row.fsd })),
                () => "var(--msg-success-text)"
            );
        },
    };
}
