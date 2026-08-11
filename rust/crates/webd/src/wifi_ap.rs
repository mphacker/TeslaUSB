//! `GET/POST /api/wifi/ap*` — the Wi-Fi AP status + control-plane proxy.
//! `webd` is a **pure proxy** for these handlers: each forwards a `cmd`-tagged
//! JSON request to `wifid` over its control socket (see
//! [`crate::wifid_client`]) and relays the answer.

use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::AppState;
use crate::error::ApiError;
use crate::gadget::TransportError;

/// The Wi-Fi AP sub-routes, mounted under `/api` by [`crate::route`].
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/wifi/ap", get(get_ap_status))
        .route("/wifi/ap/mode", post(set_ap_mode))
        .route("/wifi/ap/config", post(set_ap_config))
}

/// `GET /api/wifi/ap`: relay the full `WifiStatus` payload (`.ap` included).
async fn get_ap_status(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "get_ap_status" })).await?;
    Ok(Json(resp))
}

#[derive(Deserialize)]
struct ModeBody {
    mode: String,
}

/// `POST /api/wifi/ap/mode`: set AP mode (`auto|force_on|force_off`).
async fn set_ap_mode(
    State(state): State<AppState>,
    Json(body): Json<ModeBody>,
) -> Result<Json<Value>, ApiError> {
    if !matches!(body.mode.as_str(), "auto" | "force_on" | "force_off") {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_mode",
            "mode must be auto, force_on, or force_off",
        ));
    }
    let resp = call(&state, json!({ "cmd": "set_ap_mode", "mode": body.mode })).await?;
    Ok(Json(resp))
}

#[derive(Deserialize)]
struct ConfigBody {
    ssid: String,
    passphrase: String,
}

/// `POST /api/wifi/ap/config`: set AP SSID + passphrase.
async fn set_ap_config(
    State(state): State<AppState>,
    Json(body): Json<ConfigBody>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(
        &state,
        json!({
            "cmd": "set_ap_config",
            "ssid": body.ssid,
            "passphrase": body.passphrase
        }),
    )
    .await?;
    Ok(Json(resp))
}

/// Forward one request to `wifid` on a blocking task, relaying the JSON answer
/// or mapping the `{error:{code,message}}` envelope / transport failure onto an
/// [`ApiError`].
async fn call(state: &AppState, request: Value) -> Result<Value, ApiError> {
    let client = state.wifid.clone();
    let join = tokio::task::spawn_blocking(move || client.call(request)).await;

    let resp = match join {
        Ok(Ok(value)) => value,
        Ok(Err(TransportError::Unavailable(_))) => {
            return Err(ApiError::status(
                StatusCode::SERVICE_UNAVAILABLE,
                "wifi_ap_unavailable",
                "the wifi AP service is not reachable",
            ));
        }
        Ok(Err(TransportError::Protocol(_))) => {
            return Err(ApiError::status(
                StatusCode::BAD_GATEWAY,
                "wifi_ap_protocol",
                "the wifi AP service returned an unreadable reply",
            ));
        }
        Err(_) => return Err(ApiError::Internal),
    };

    if let Some(err) = resp.get("error") {
        let code = err
            .get("code")
            .and_then(Value::as_str)
            .unwrap_or("wifi_ap_error")
            .to_owned();
        let message = err
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("wifi AP error")
            .to_owned();
        return Err(ApiError::upstream(status_for_ap(&code), code, message));
    }
    Ok(resp)
}

fn status_for_ap(code: &str) -> StatusCode {
    match code {
        "invalid_argument" => StatusCode::UNPROCESSABLE_ENTITY,
        "unavailable" => StatusCode::SERVICE_UNAVAILABLE,
        "timeout" => StatusCode::GATEWAY_TIMEOUT,
        _ => StatusCode::BAD_GATEWAY,
    }
}
