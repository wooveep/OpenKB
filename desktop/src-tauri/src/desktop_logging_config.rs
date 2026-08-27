//! Startup-only Portable Local Configuration for Desktop diagnostics.

use serde_json::Value;
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub(crate) const CONFIG_FILE_NAME: &str = "openkb.local.json";
pub(crate) const DIAGNOSTIC_COMPONENTS: [&str; 11] = [
    "shell",
    "bridge",
    "runtime",
    "import",
    "parser",
    "model",
    "page_tree",
    "retrieval",
    "knowledge",
    "projection",
    "storage",
];

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum LogLevel {
    Trace = 5,
    Debug = 10,
    Info = 20,
    Warn = 30,
    Error = 40,
}

impl LogLevel {
    pub(crate) fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_uppercase().as_str() {
            "TRACE" => Some(Self::Trace),
            "DEBUG" => Some(Self::Debug),
            "INFO" => Some(Self::Info),
            "WARN" | "WARNING" => Some(Self::Warn),
            "ERROR" => Some(Self::Error),
            _ => None,
        }
    }

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Trace => "TRACE",
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warn => "WARN",
            Self::Error => "ERROR",
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct LoggingConfig {
    pub(crate) level: LogLevel,
    pub(crate) components: BTreeMap<String, LogLevel>,
    pub(crate) runtime_session_id: String,
    pub(crate) config_path: PathBuf,
    pub(crate) log_directory: PathBuf,
    pub(crate) sensitive_trace_root: PathBuf,
    pub(crate) sensitive_trace_capture_id: Option<String>,
    pub(crate) sensitive_trace_expires_at_text: Option<String>,
    sensitive_trace_expires_at: Option<SystemTime>,
    pub(crate) sensitive_trace_stop_file: Option<PathBuf>,
    pub(crate) allow_sensitive_trace: bool,
    pub(crate) warnings: Vec<String>,
}

impl LoggingConfig {
    pub(crate) fn effective_level(&self, component: &str) -> LogLevel {
        if !self.trace_components().is_empty() && !self.sensitive_trace_active() {
            return LogLevel::Warn;
        }
        self.components
            .get(component)
            .copied()
            .unwrap_or(self.level)
    }

    pub(crate) fn trace_components(&self) -> Vec<String> {
        DIAGNOSTIC_COMPONENTS
            .iter()
            .filter(|component| {
                self.components
                    .get(**component)
                    .copied()
                    .unwrap_or(self.level)
                    == LogLevel::Trace
            })
            .map(|component| (*component).to_owned())
            .collect()
    }

    pub(crate) fn sensitive_trace_active(&self) -> bool {
        if !self.allow_sensitive_trace
            || self.sensitive_trace_capture_id.is_none()
            || self
                .sensitive_trace_expires_at
                .is_none_or(|expiry| SystemTime::now() >= expiry)
        {
            return false;
        }
        self.sensitive_trace_stop_file
            .as_ref()
            .is_none_or(|path| !path.exists())
    }

    pub(crate) fn engine_environment(&self) -> Vec<(String, String)> {
        let components = self
            .components
            .iter()
            .map(|(component, level)| (component.clone(), Value::from(level.as_str())))
            .collect::<serde_json::Map<_, _>>();
        let mut values = vec![
            (
                "OPENKB_LOG_LEVEL".to_owned(),
                self.level.as_str().to_owned(),
            ),
            (
                "OPENKB_LOG_COMPONENT_LEVELS".to_owned(),
                Value::Object(components).to_string(),
            ),
            (
                "OPENKB_RUNTIME_SESSION_ID".to_owned(),
                self.runtime_session_id.clone(),
            ),
            (
                "OPENKB_LOG_DIR".to_owned(),
                self.log_directory.to_string_lossy().into_owned(),
            ),
            (
                "OPENKB_ALLOW_SENSITIVE_TRACE".to_owned(),
                self.allow_sensitive_trace.to_string(),
            ),
            (
                "OPENKB_SENSITIVE_TRACE_ROOT".to_owned(),
                self.sensitive_trace_root.to_string_lossy().into_owned(),
            ),
        ];
        if let Some(value) = &self.sensitive_trace_capture_id {
            values.push((
                "OPENKB_SENSITIVE_TRACE_CAPTURE_ID".to_owned(),
                value.clone(),
            ));
        }
        if let Some(value) = &self.sensitive_trace_expires_at_text {
            values.push((
                "OPENKB_SENSITIVE_TRACE_EXPIRES_AT".to_owned(),
                value.clone(),
            ));
        }
        if let Some(value) = &self.sensitive_trace_stop_file {
            values.push((
                "OPENKB_SENSITIVE_TRACE_STOP_FILE".to_owned(),
                value.to_string_lossy().into_owned(),
            ));
        }
        values
    }
}

pub(crate) fn load_logging_config(local_state_root: PathBuf) -> LoggingConfig {
    let resolved_config_path = env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .map(|directory| directory.join(CONFIG_FILE_NAME));
    let config_path = resolved_config_path
        .clone()
        .unwrap_or_else(|| PathBuf::from(CONFIG_FILE_NAME));
    let (source, read_warning) = match resolved_config_path {
        Some(path) => match fs::read_to_string(path) {
            Ok(source) => (Some(source), None),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (None, None),
            Err(_) => (None, Some("portable_configuration_read_failed")),
        },
        None => (None, Some("portable_configuration_location_unavailable")),
    };
    let mut config = parse_logging_config(
        source.as_deref(),
        config_path,
        local_state_root,
        SystemTime::now(),
    );
    if let Some(warning) = read_warning {
        config.warnings.push(warning.to_owned());
    }
    config
}

fn parse_logging_config(
    source: Option<&str>,
    config_path: PathBuf,
    local_state_root: PathBuf,
    now: SystemTime,
) -> LoggingConfig {
    let mut level = LogLevel::Warn;
    let mut components = BTreeMap::new();
    let mut allow_sensitive_trace = false;
    let mut expires_at_text = None;
    let mut expires_at = None;
    let mut warnings = Vec::new();
    let mut configuration_invalid = false;
    if let Some(source) = source {
        match serde_json::from_str::<Value>(source) {
            Ok(Value::Object(root)) => {
                for _ in root.keys().filter(|key| key.as_str() != "logging") {
                    warnings.push("unknown_config_field".to_owned());
                }
                if let Some(logging) = root.get("logging") {
                    if let Value::Object(values) = logging {
                        let known = [
                            "level",
                            "components",
                            "allow_sensitive_trace",
                            "sensitive_trace_expires_at",
                        ];
                        for _ in values.keys().filter(|key| !known.contains(&key.as_str())) {
                            warnings.push("unknown_logging_field".to_owned());
                        }
                        if let Some(value) = values.get("level") {
                            if let Some(parsed) = value.as_str().and_then(LogLevel::parse) {
                                level = parsed;
                            } else {
                                warnings.push("logging_level_invalid".to_owned());
                                configuration_invalid = true;
                            }
                        }
                        configuration_invalid |= parse_components(
                            values.get("components"),
                            &mut components,
                            &mut warnings,
                        );
                        if let Some(value) = values.get("allow_sensitive_trace") {
                            if let Some(parsed) = value.as_bool() {
                                allow_sensitive_trace = parsed;
                            } else {
                                warnings.push("allow_sensitive_trace_invalid".to_owned());
                                configuration_invalid = true;
                            }
                        }
                        if let Some(value) = values.get("sensitive_trace_expires_at") {
                            if let Some(text) = value.as_str() {
                                expires_at_text = Some(text.to_owned());
                                expires_at = parse_utc_timestamp(text);
                            } else {
                                warnings.push("sensitive_trace_expiry_invalid".to_owned());
                                configuration_invalid = true;
                            }
                        }
                    } else {
                        warnings.push("logging_configuration_invalid".to_owned());
                        configuration_invalid = true;
                    }
                }
            }
            _ => {
                warnings.push("portable_configuration_invalid".to_owned());
                configuration_invalid = true;
            }
        }
    }

    if expires_at_text.is_some() && expires_at.is_none() {
        warnings.push("sensitive_trace_expiry_invalid".to_owned());
        configuration_invalid = true;
    }
    if configuration_invalid {
        level = LogLevel::Warn;
        components.clear();
        allow_sensitive_trace = false;
        expires_at_text = None;
        expires_at = None;
    }

    let trace_requested = level == LogLevel::Trace
        || components
            .values()
            .any(|configured| *configured == LogLevel::Trace);
    let authorization_valid = allow_sensitive_trace
        && expires_at.is_some_and(|expiry| {
            expiry > now
                && expiry
                    .duration_since(now)
                    .is_ok_and(|duration| duration <= Duration::from_secs(24 * 60 * 60))
        });
    if trace_requested && !authorization_valid {
        level = LogLevel::Warn;
        components.clear();
        allow_sensitive_trace = false;
        expires_at_text = None;
        expires_at = None;
        warnings.push("sensitive_trace_authorization_invalid".to_owned());
    }
    let runtime_session_id = unique_id("runtime", now);
    let capture_id = (trace_requested && authorization_valid).then(|| unique_id("capture", now));
    let sensitive_trace_root = local_state_root.join("sensitive-traces");
    let stop_file = capture_id
        .as_ref()
        .map(|capture_id| sensitive_trace_root.join(capture_id).join(".stop"));
    warnings.sort();
    warnings.dedup();
    LoggingConfig {
        level,
        components,
        runtime_session_id,
        config_path,
        log_directory: local_state_root.join("logs"),
        sensitive_trace_root,
        sensitive_trace_capture_id: capture_id,
        sensitive_trace_expires_at_text: expires_at_text,
        sensitive_trace_expires_at: expires_at,
        sensitive_trace_stop_file: stop_file,
        allow_sensitive_trace,
        warnings,
    }
}

fn parse_components(
    value: Option<&Value>,
    components: &mut BTreeMap<String, LogLevel>,
    warnings: &mut Vec<String>,
) -> bool {
    let Some(value) = value else { return false };
    let Value::Object(values) = value else {
        warnings.push("logging_component_levels_invalid".to_owned());
        return true;
    };
    let mut invalid = false;
    for (component, value) in values {
        if !DIAGNOSTIC_COMPONENTS.contains(&component.as_str()) {
            warnings.push("unknown_logging_component".to_owned());
            continue;
        }
        if let Some(level) = value.as_str().and_then(LogLevel::parse) {
            components.insert(component.clone(), level);
        } else {
            warnings.push(format!("logging_component_level_invalid:{component}"));
            invalid = true;
        }
    }
    invalid
}

fn unique_id(prefix: &str, now: SystemTime) -> String {
    let elapsed = now.duration_since(UNIX_EPOCH).unwrap_or_default();
    format!(
        "{prefix}-{}-{}-{}",
        elapsed.as_secs(),
        elapsed.subsec_nanos(),
        std::process::id()
    )
}

fn parse_utc_timestamp(value: &str) -> Option<SystemTime> {
    let normalized = value
        .strip_suffix('Z')
        .or_else(|| value.strip_suffix("+00:00"))?;
    let (date, time) = normalized.split_once('T')?;
    let mut date_parts = date.split('-').map(str::parse::<i64>);
    let year = date_parts.next()?.ok()?;
    let month = date_parts.next()?.ok()?;
    let day = date_parts.next()?.ok()?;
    if date_parts.next().is_some() || !(1970..=9999).contains(&year) {
        return None;
    }
    let time = match time.split_once('.') {
        None => time,
        Some((whole, fraction))
            if !fraction.is_empty()
                && fraction.len() <= 9
                && fraction.chars().all(|character| character.is_ascii_digit()) =>
        {
            whole
        }
        Some(_) => return None,
    };
    let mut time_parts = time.split(':').map(str::parse::<i64>);
    let hour = time_parts.next()?.ok()?;
    let minute = time_parts.next()?.ok()?;
    let second = time_parts.next()?.ok()?;
    if time_parts.next().is_some()
        || !(1..=12).contains(&month)
        || day < 1
        || day > days_in_month(year, month)
        || !(0..=23).contains(&hour)
        || !(0..=59).contains(&minute)
        || !(0..=59).contains(&second)
    {
        return None;
    }
    let days = days_from_civil(year, month, day);
    let seconds = days
        .checked_mul(86_400)?
        .checked_add(hour * 3_600 + minute * 60 + second)?;
    u64::try_from(seconds)
        .ok()
        .map(|seconds| UNIX_EPOCH + Duration::from_secs(seconds))
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(1_787_788_800) // 2026-08-27T00:00:00Z
    }

    #[test]
    fn defaults_to_warn_without_a_configuration_file() {
        let config =
            parse_logging_config(None, "app/openkb.local.json".into(), "state".into(), now());
        assert_eq!(config.level, LogLevel::Warn);
        assert_eq!(config.effective_level("model"), LogLevel::Warn);
        assert!(config.warnings.is_empty());
    }

    #[test]
    fn parses_global_and_component_levels_case_insensitively() {
        let config = parse_logging_config(
            Some(r#"{"logging":{"level":"info","components":{"import":"debug"}}}"#),
            "app/openkb.local.json".into(),
            "state".into(),
            now(),
        );
        assert_eq!(config.effective_level("runtime"), LogLevel::Info);
        assert_eq!(config.effective_level("import"), LogLevel::Debug);
    }

    #[test]
    fn trace_requires_true_consent_and_an_expiry_within_twenty_four_hours() {
        let rejected = parse_logging_config(
            Some(r#"{"logging":{"level":"TRACE"}}"#),
            "app/openkb.local.json".into(),
            "state".into(),
            now(),
        );
        let accepted = parse_logging_config(
            Some(
                r#"{"logging":{"level":"WARN","components":{"model":"TRACE"},"allow_sensitive_trace":true,"sensitive_trace_expires_at":"2026-08-27T01:00:00Z"}}"#,
            ),
            "app/openkb.local.json".into(),
            "state".into(),
            now(),
        );
        assert_eq!(rejected.level, LogLevel::Warn);
        assert!(rejected
            .warnings
            .contains(&"sensitive_trace_authorization_invalid".to_owned()));
        assert_eq!(accepted.components.get("model"), Some(&LogLevel::Trace));
        assert!(accepted.sensitive_trace_capture_id.is_some());
    }

    #[test]
    fn unknown_fields_and_components_are_ignored_with_stable_warnings() {
        let config = parse_logging_config(
            Some(r#"{"future":1,"logging":{"future":2,"components":{"future":"DEBUG"}}}"#),
            "app/openkb.local.json".into(),
            "state".into(),
            now(),
        );
        assert_eq!(config.level, LogLevel::Warn);
        assert_eq!(config.warnings.len(), 3);
    }

    #[test]
    fn invalid_known_values_fail_closed_to_all_warn() {
        let config = parse_logging_config(
            Some(
                r#"{"logging":{"level":"DEBUG","components":{"model":"verbose"},"allow_sensitive_trace":"yes"}}"#,
            ),
            "app/openkb.local.json".into(),
            "state".into(),
            now(),
        );
        assert_eq!(config.level, LogLevel::Warn);
        assert!(config.components.is_empty());
        assert!(!config.allow_sensitive_trace);
        assert_eq!(config.effective_level("model"), LogLevel::Warn);
    }

    #[test]
    fn utc_expiry_parser_rejects_malformed_fractional_seconds() {
        assert!(parse_utc_timestamp("2026-08-27T01:00:00.123Z").is_some());
        assert!(parse_utc_timestamp("2026-08-27T01:00:00.secretZ").is_none());
        assert!(parse_utc_timestamp("2026-08-27T01:00:00.Z").is_none());
    }
}
