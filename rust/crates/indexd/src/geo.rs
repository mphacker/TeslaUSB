//! Pure geometry helpers for trip derivation.
//!
//! Ported verbatim (constants + algorithm) from v1's
//! `web/teslausb_web/services/mapping_trip_derivation.py` so the derived
//! distances and simplified polylines match what a user sees today. The
//! v1 production materializer (`teslausb-worker`) uses the same haversine
//! constant (`6371.0088`).

/// Mean Earth radius in kilometres (`mapping_trip_derivation.py`
/// `_EARTH_RADIUS_KM`; matches the materializer's `EARTH_KM`).
pub const EARTH_RADIUS_KM: f64 = 6371.0088;

/// Metres per degree of latitude used by the equirectangular projection
/// in `_project_polyline_to_xy` (`deg_lat_m = 111_320.0`).
pub const DEG_LAT_M: f64 = 111_320.0;

/// RDP needs at least this many points to simplify; below it every point
/// is kept (`_MIN_RDP_POINTS`).
const MIN_RDP_POINTS: usize = 3;

/// A renderable run needs at least this many points (`_MIN_RENDERABLE_POINTS`).
const MIN_RENDERABLE_POINTS: usize = 2;

/// The two mandatory endpoints kept by [`cap_indices_uniform`]
/// (`_MANDATORY_ENDPOINT_COUNT`).
const MANDATORY_ENDPOINT_COUNT: usize = 2;

/// Great-circle distance between two lat/lon pairs in kilometres.
///
/// Verbatim port of `mapping_trip_derivation.py::haversine_km`.
#[must_use]
pub fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let radius_lat1 = lat1.to_radians();
    let radius_lat2 = lat2.to_radians();
    let delta_lat = (lat2 - lat1).to_radians();
    let delta_lon = (lon2 - lon1).to_radians();
    let a = (delta_lat / 2.0).sin().powi(2)
        + radius_lat1.cos() * radius_lat2.cos() * (delta_lon / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_KM * a.sqrt().asin()
}

/// Ramer–Douglas–Peucker simplification: returns the indices to keep.
///
/// Verbatim port of `mapping_trip_derivation.py::simplify_polyline_rdp`
/// (iterative stack form). `epsilon_m` is the perpendicular-distance
/// tolerance in metres; v1 uses `8.0` (`_DEFAULT_EPSILON_METERS`).
#[must_use]
pub fn simplify_polyline_rdp(latlons: &[(f64, f64)], epsilon_m: f64) -> Vec<usize> {
    if latlons.len() < MIN_RDP_POINTS {
        return (0..latlons.len()).collect();
    }
    let projected = project_polyline_to_xy(latlons);
    let last = latlons.len() - 1;
    let mut keep = vec![false; latlons.len()];
    if let Some(first) = keep.first_mut() {
        *first = true;
    }
    if let Some(end) = keep.get_mut(last) {
        *end = true;
    }
    let epsilon_sq = epsilon_m * epsilon_m;
    let mut stack: Vec<(usize, usize)> = vec![(0, last)];
    while let Some((start, end)) = stack.pop() {
        if let Some(farthest) = farthest_point_index(&projected, start, end, epsilon_sq) {
            if let Some(slot) = keep.get_mut(farthest) {
                *slot = true;
            }
            stack.push((start, farthest));
            stack.push((farthest, end));
        }
    }
    keep.iter()
        .enumerate()
        .filter_map(|(index, &kept)| kept.then_some(index))
        .collect()
}

/// Down-sample `indices` uniformly so the result never exceeds
/// `max_count`, always keeping the two endpoints.
///
/// Verbatim port of `mapping_trip_derivation.py::cap_indices_uniform`.
#[must_use]
pub fn cap_indices_uniform(indices: &[usize], max_count: usize) -> Vec<usize> {
    let max_count = max_count.max(MIN_RENDERABLE_POINTS);
    if indices.len() <= max_count {
        return indices.to_vec();
    }
    let (Some(&first), Some(&last)) = (indices.first(), indices.last()) else {
        return indices.to_vec();
    };
    let interior = indices
        .get(1..indices.len().saturating_sub(1))
        .unwrap_or(&[]);
    let extras_count = max_count - MANDATORY_ENDPOINT_COUNT;
    if extras_count == 0 || interior.is_empty() {
        return vec![first, last];
    }
    // `step = len(interior) / extras_count`; pick `interior[int(i*step)]`
    // for i in 0..extras_count, clamped to the last interior index.
    #[allow(clippy::cast_precision_loss)]
    let step = interior.len() as f64 / extras_count as f64;
    let mut chosen: Vec<usize> = vec![first, last];
    for i in 0..extras_count {
        #[allow(
            clippy::cast_precision_loss,
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss
        )]
        let raw = (i as f64 * step) as usize;
        let idx = raw.min(interior.len() - 1);
        if let Some(&value) = interior.get(idx) {
            chosen.push(value);
        }
    }
    chosen.sort_unstable();
    chosen.dedup();
    chosen
}

/// Project lat/lon to a local equirectangular metre plane centred on the
/// mean latitude. Returns `(x = lon·m_per_deg_lon, y = lat·m_per_deg_lat)`
/// — verbatim port of `_project_polyline_to_xy`.
fn project_polyline_to_xy(latlons: &[(f64, f64)]) -> Vec<(f64, f64)> {
    #[allow(clippy::cast_precision_loss)]
    let count = latlons.len() as f64;
    let mean_lat = latlons.iter().map(|&(lat, _)| lat).sum::<f64>() / count;
    let cos_lat = mean_lat.to_radians().cos();
    let deg_lon_m = DEG_LAT_M * cos_lat;
    latlons
        .iter()
        .map(|&(lat, lon)| (lon * deg_lon_m, lat * DEG_LAT_M))
        .collect()
}

/// Index of the point farthest from the `start..end` segment whose
/// squared perpendicular distance exceeds `epsilon_sq`, else `None`.
fn farthest_point_index(
    projected: &[(f64, f64)],
    start: usize,
    end: usize,
    epsilon_sq: f64,
) -> Option<usize> {
    if end <= start + 1 {
        return None;
    }
    let seg_start = *projected.get(start)?;
    let seg_end = *projected.get(end)?;
    let mut max_distance_sq = 0.0_f64;
    let mut farthest: Option<usize> = None;
    for (index, &point) in projected.iter().enumerate().take(end).skip(start + 1) {
        let distance_sq = distance_sq_to_segment(point, seg_start, seg_end);
        if distance_sq > max_distance_sq {
            max_distance_sq = distance_sq;
            farthest = Some(index);
        }
    }
    if max_distance_sq > epsilon_sq {
        farthest
    } else {
        None
    }
}

/// Squared distance from `point` to the segment `start..end`, verbatim
/// port of `_distance_sq_to_segment`.
fn distance_sq_to_segment(point: (f64, f64), start: (f64, f64), end: (f64, f64)) -> f64 {
    let (x1, y1) = start;
    let (x2, y2) = end;
    let (px, py) = point;
    let dx = x2 - x1;
    let dy = y2 - y1;
    let denom = dx * dx + dy * dy;
    if denom == 0.0 {
        return (px - x1).powi(2) + (py - y1).powi(2);
    }
    let numerator = dy * px - dx * py + x2 * y1 - y2 * x1;
    (numerator * numerator) / denom
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp, clippy::unwrap_used)]

    use super::{cap_indices_uniform, haversine_km, simplify_polyline_rdp};

    #[test]
    fn haversine_zero_distance() {
        assert_eq!(haversine_km(40.0, -75.0, 40.0, -75.0), 0.0);
    }

    #[test]
    fn haversine_known_one_degree_latitude() {
        // One degree of latitude ≈ 111.19 km on this mean-radius sphere.
        let d = haversine_km(0.0, 0.0, 1.0, 0.0);
        assert!((d - 111.195).abs() < 0.01, "got {d}");
    }

    #[test]
    fn rdp_keeps_all_when_below_min_points() {
        let pts = [(0.0, 0.0), (1.0, 1.0)];
        assert_eq!(simplify_polyline_rdp(&pts, 8.0), vec![0, 1]);
    }

    #[test]
    fn rdp_drops_collinear_interior_point() {
        // Three colinear points: the middle one is within epsilon of the
        // line and must be dropped.
        let pts = [(0.0, 0.0), (0.0, 0.5), (0.0, 1.0)];
        assert_eq!(simplify_polyline_rdp(&pts, 8.0), vec![0, 2]);
    }

    #[test]
    fn rdp_keeps_sharp_corner() {
        // A clear right-angle corner is far from the chord and is kept.
        let pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)];
        assert_eq!(simplify_polyline_rdp(&pts, 8.0), vec![0, 1, 2]);
    }

    #[test]
    fn cap_returns_input_when_within_limit() {
        let idx = [0, 1, 2, 3];
        assert_eq!(cap_indices_uniform(&idx, 200), vec![0, 1, 2, 3]);
    }

    #[test]
    fn cap_keeps_endpoints_and_downsamples() {
        let idx: Vec<usize> = (0..10).collect();
        let capped = cap_indices_uniform(&idx, 4);
        assert_eq!(capped.first().copied(), Some(0));
        assert_eq!(capped.last().copied(), Some(9));
        assert!(capped.len() <= 4, "len {}", capped.len());
        let mut sorted = capped.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(capped, sorted);
    }
}
