use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
use reqwest::header::{HeaderMap, HeaderValue};
use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::events::{
    events_from_feed, gateway_event_from_sse, RuntimeEvent, TimelineSnapshot,
};
use crate::live::LiveSnapshot;

#[derive(Clone)]
pub struct ApiClient {
    base_url: String,
    api_key: String,
    http: reqwest::Client,
}

impl ApiClient {
    pub fn new(base_url: String, api_key: String) -> Result<Self> {
        let mut headers = HeaderMap::new();
        headers.insert(
            "X-API-Key",
            HeaderValue::from_str(&api_key).context("invalid API key header")?,
        );
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .danger_accept_invalid_certs(true)
            .build()
            .context("failed to build HTTP client")?;
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key,
            http,
        })
    }

    pub async fn stream_query(
        &self,
        query: &str,
        model: &str,
        cwd: &str,
        session_id: &str,
        tx: mpsc::Sender<RuntimeEvent>,
    ) -> Result<Option<String>> {
        let payload = json!({
            "query": query,
            "model": model,
            "stream": true,
            "client_cwd": cwd,
            "session_id": session_id,
        });
        let url = format!("{}/query", self.base_url);
        let response = self
            .http
            .post(url)
            .json(&payload)
            .send()
            .await
            .context("failed to send /query request to Symbiont API")?;
        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            bail!("/query failed with {status}: {}", body.trim());
        }

        let mut task_id: Option<String> = None;
        let mut current_event = String::new();
        let mut buffer = String::new();
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.context("failed while reading /query stream")?;
            buffer.push_str(&String::from_utf8_lossy(&chunk));
            while let Some(index) = buffer.find('\n') {
                let line: String = buffer.drain(..=index).collect();
                let line = line.trim_end_matches(['\n', '\r']);
                if line.is_empty() {
                    current_event.clear();
                    continue;
                }
                if let Some(kind) = line.strip_prefix("event:") {
                    current_event = kind.trim().to_string();
                    continue;
                }
                if let Some(data) = line.strip_prefix("data:") {
                    let event = gateway_event_from_sse(&current_event, data.trim_start());
                    if let crate::events::GatewayEvent::Agentic { task_id: id, .. } = &event {
                        task_id = Some(id.clone());
                    }
                    if tx.send(RuntimeEvent::Gateway(event)).await.is_err() {
                        return Ok(task_id);
                    }
                }
            }
        }
        Ok(task_id)
    }

    pub async fn fetch_timeline(&self, task_id: &str) -> Result<TimelineSnapshot> {
        let url = format!("{}/agentic/tasks/{}/timeline", self.base_url, task_id);
        let value: Value = self
            .http
            .get(url)
            .send()
            .await
            .context("failed to fetch task timeline from Symbiont API")?
            .error_for_status()
            .context("task timeline endpoint returned an error")?
            .json()
            .await
            .context("failed to decode task timeline")?;
        Ok(TimelineSnapshot::from_value(&value))
    }

    pub async fn fetch_events(&self, task_id: &str, cursor: u64) -> Result<Vec<crate::events::EventLine>> {
        let url = format!(
            "{}/agentic/tasks/{}/events?cursor={}",
            self.base_url, task_id, cursor
        );
        let value: Value = self
            .http
            .get(url)
            .send()
            .await
            .context("failed to fetch task events from Symbiont API")?
            .error_for_status()
            .context("task events endpoint returned an error")?
            .json()
            .await
            .context("failed to decode task events")?;
        Ok(events_from_feed(&value))
    }

    pub async fn fetch_live_snapshot(&self, status: &str, limit: usize, recent_seconds: u64) -> Result<LiveSnapshot> {
        let url = format!(
            "{}/agentic/live/snapshot?status={}&limit={}&recent_seconds={}",
            self.base_url,
            url_encode(status),
            limit,
            recent_seconds
        );
        let value: Value = self
            .http
            .get(url)
            .send()
            .await
            .context("failed to fetch live snapshot from Symbiont API")?
            .error_for_status()
            .context("live snapshot endpoint returned an error")?
            .json()
            .await
            .context("failed to decode live snapshot")?;
        Ok(LiveSnapshot::from_value(&value))
    }

    pub fn api_key_present(&self) -> bool {
        !self.api_key.is_empty()
    }
}

fn url_encode(value: &str) -> String {
    value
        .bytes()
        .flat_map(|byte| match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => vec![byte as char],
            other => format!("%{other:02X}").chars().collect(),
        })
        .collect()
}
