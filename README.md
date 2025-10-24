```rust
# rust-encryption-uploader/Cargo.toml
[workspace]
members = [
  "crates/app_gui",
  "crates/detector_core",
  "crates/excel_io",
  "crates/crypto",
  "crates/api_client"
]
resolver = "2"

[workspace.package]
edition = "2021"

[workspace.dependencies]
anyhow = "1"
thiserror = "1"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
log = "0.4"

egui = "0.27"
eframe = { version = "0.27", default-features = true, features = ["glow"] }
rfd = "0.14"

calamine = "0.22"
csv = "1.3"
regex = "1"
rand = "0.8"
rand_core = "0.6"
base64 = "0.22"

dotenvy = "0.15"
confy = "0.6"

aead = "0.5"
chacha20poly1305 = { version = "0.10", features = ["alloc"] }
sha2 = "0.10"

time = { version = "0.3", features = ["formatting"] }

reqwest = { version = "0.12", default-features = false, features = ["json","blocking","gzip","brotli","deflate","rustls-tls"] }
uuid = { version = "1", features = ["v4","fast-rng"] }

# ===== crates/app_gui/Cargo.toml =====
[package]
name = "app_gui"
version = "0.1.0"
edition = "2021"

[dependencies]
anyhow = { workspace = true }
log = { workspace = true }
serde = { workspace = true }
serde_json = { workspace = true }
egui = { workspace = true }
eframe = { workspace = true }
rfd = { workspace = true }
dotenvy = { workspace = true }
confy = { workspace = true }

detector_core = { path = "../detector_core" }
excel_io = { path = "../excel_io" }
crypto = { package = "crypto", path = "../crypto" }
api_client = { path = "../api_client" }

# ===== crates/app_gui/src/main.rs =====
use anyhow::Result;
use eframe::{egui, egui::{RichText, ScrollArea}};
use std::collections::HashMap;
use std::path::PathBuf;

use detector_core::{FieldKind, ColumnGuess, guess_columns, mask_pan_for_ui};
use excel_io::open_any;
use crypto::{encrypt_chunk, ContentKey};
use api_client::UploadClient;

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct AppCfg {
    site: String,
    username: String,
    api_base: String,
    port: u16,
}

impl Default for AppCfg {
    fn default() -> Self {
        Self { site: String::new(), username: String::new(), api_base: String::new(), port: 443 }
    }
}

struct PreviewFrame {
    headers: Vec<String>,
    rows: Vec<Vec<String>>, // masked for UI
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum UiStep { Connect, Map }

struct MyApp {
    cfg: AppCfg,
    password: String, // not persisted
    file_path: Option<PathBuf>,
    preview: Option<PreviewFrame>,
    guesses: Vec<ColumnGuess>,
    mapping: HashMap<usize, FieldKind>,
    custom_titles: HashMap<usize, String>,
    status: String,
    step: UiStep,
    sites: Vec<String>,
}

impl Default for MyApp {
    fn default() -> Self {
        let _ = dotenvy::dotenv();
        let mut cfg: AppCfg = confy::load("rust-encryption-uploader", None::<&str>).unwrap_or_default();
        if cfg.api_base.is_empty() {
            cfg.api_base = std::env::var("UPLOADER_API_BASE").unwrap_or_else(|_| "https://example.test".into());
        }
        if cfg.site.is_empty() {
            cfg.site = std::env::var("UPLOADER_SITE").unwrap_or_default();
        }
        if cfg.username.is_empty() {
            cfg.username = std::env::var("UPLOADER_USERNAME").unwrap_or_default();
        }
        if cfg.port == 0 {
            cfg.port = std::env::var("UPLOADER_PORT").ok().and_then(|s| s.parse::<u16>().ok()).unwrap_or(443);
        }
        let sites = std::env::var("UPLOADER_SITES").unwrap_or_default()
            .split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect::<Vec<_>>();
        Self { cfg, password: String::new(), file_path: None, preview: None, guesses: vec![], mapping: HashMap::new(), custom_titles: HashMap::new(), status: String::new(), step: UiStep::Connect, sites }
    }
}

impl eframe::App for MyApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        egui::TopBottomPanel::top("top").show(ctx, |ui| {
            ui.heading(RichText::new("Rust Encryption Uploader").strong());
            ui.separator();
        });

        egui::SidePanel::left("left").resizable(true).default_width(360.0).show(ctx, |ui| {
            ui.heading("Connection");
            // Site dropdown if provided via .env, else free text
            ui.horizontal(|ui| {
                ui.label("Site");
                if self.sites.is_empty() {
                    ui.text_edit_singleline(&mut self.cfg.site);
                } else {
                    egui::ComboBox::from_id_source("site_combo")
                        .selected_text(if self.cfg.site.is_empty() { "Select site".to_string() } else { self.cfg.site.clone() })
                        .show_ui(ui, |ui| {
                            for s in &self.sites {
                                let sel = self.cfg.site == *s;
                                if ui.selectable_label(sel, s).clicked() { self.cfg.site = s.clone(); }
                            }
                        });
                }
            });
            ui.horizontal(|ui| { ui.label("API Base"); ui.text_edit_singleline(&mut self.cfg.api_base); });
            ui.horizontal(|ui| { ui.label("Port"); ui.add(egui::DragValue::new(&mut self.cfg.port).clamp_range(1..=65535)); });
            ui.horizontal(|ui| { ui.label("Username"); ui.text_edit_singleline(&mut self.cfg.username); });
            ui.horizontal(|ui| { ui.label("Password"); let mut pw = egui::text::TextEdit::singleline(&mut self.password); pw = pw.password(true); ui.add(pw); });
            if ui.button("Save non-secrets").clicked() {
                if let Err(e) = confy::store("rust-encryption-uploader", None::<&str>, &self.cfg) { self.status = format!("Save error: {}", e); } else { self.status = "Saved".into(); }
            }
            ui.separator();
            ui.horizontal(|ui| {
                if ui.button("Next →").clicked() {
                    if self.file_path.is_none() {
                        if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() {
                            self.load_preview(path);
                            self.step = UiStep::Map;
                        }
                    } else { self.step = UiStep::Map; }
                }
                if ui.button("Pick file…").clicked() {
                    if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() { self.load_preview(path); }
                }
            });
            if let Some(p) = &self.file_path { ui.label(format!("File: {}", p.display())); }
            ui.separator();
            ui.label(RichText::new(&self.status).monospace());
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            match self.step {
                UiStep::Connect => { ui.label("Fill in connection details, then click Next →"); },
                UiStep::Map => self.map_view(ui),
            }
        });
    }
}

impl MyApp {
    fn map_view(&mut self, ui: &mut egui::Ui) {
        ui.heading("Preview & Assign Titles");
        if let Some(prev) = &self.preview {
            // Row 0: assumed titles (from mapping or custom)
            ui.spacing_mut().item_spacing.y = 6.0;
            ui.label("Assumed titles (editable below)");
            ScrollArea::horizontal().show(ui, |ui| {
                egui::Grid::new("assumed_titles").striped(true).show(ui, |ui| {
                    for (i, _) in prev.headers.iter().enumerate() {
                        let assumed = self.custom_titles.get(&i).cloned().unwrap_or_else(|| {
                            format!("{:?}", self.mapping.get(&i).copied().unwrap_or(FieldKind::Unknown))
                        });
                        ui.label(assumed);
                    }
                    ui.end_row();
                });
            });

            ui.separator();
            ui.label("Sample data (first 5 rows)");
            ScrollArea::both().auto_shrink([false; 2]).show(ui, |ui| {
                egui::Grid::new("preview_grid").striped(true).show(ui, |ui| {
                    // headers from file
                    for h in &prev.headers { ui.label(RichText::new(h)); }
                    ui.end_row();
                    // 5 rows
                    for row in prev.rows.iter().take(5) {
                        for cell in row { ui.label(cell); }
                        ui.end_row();
                    }
                });
            });

            ui.separator();
            ui.heading("Assign titles per column");
            for (idx, header) in prev.headers.iter().enumerate() {
                ui.horizontal(|ui| {
                    ui.label(format!("Col {}: {}", idx, if header.is_empty() { "(no header)" } else { header }));

                    // Dropdown of known kinds
                    let current_kind = self.mapping.get(&idx).copied().unwrap_or(FieldKind::Unknown);
                    egui::ComboBox::from_id_source(format!("kind_{}", idx))
                        .selected_text(format!("{:?}", current_kind))
                        .show_ui(ui, |ui| {
                            for k in FieldKind::iter_all() {
                                if ui.selectable_label(false, format!("{:?}", k)).clicked() { self.mapping.insert(idx, k); }
                            }
                        });

                    // Custom/export title (free text)
                    let mut title = self.custom_titles.get(&idx).cloned().unwrap_or_default();
                    ui.text_edit_singleline(&mut title);
                    if !title.is_empty() { self.custom_titles.insert(idx, title); } else { self.custom_titles.remove(&idx); }
                });
            }

            ui.separator();
            let can_upload = self.file_path.is_some() && !self.password.is_empty() && self.mapping.values().any(|k| *k != FieldKind::Unknown);
            ui.horizontal(|ui| {
                if ui.button("Validate titles").clicked() {
                    match self.validate_mapping() { Ok(msg) => self.status = msg, Err(e) => self.status = format!("Validation failed: {}", e) }
                }
                let mut btn = egui::Button::new("Encrypt & Upload");
                if !can_upload { btn = btn.sense(egui::Sense::hover()); }
                if ui.add(btn).clicked() {
                    if let Err(e) = self.encrypt_and_upload_full() { self.status = format!("Upload error: {}", e); } else { self.status = "Upload complete".into(); }
                }
            });
        } else {
            ui.label("Pick a file (Next →) to begin.");
        }
    }

    fn load_preview(&mut self, path: PathBuf) {
        self.file_path = Some(path.clone());
        match open_any(&path) {
            Ok((_, mut iter)) => {
                let headers = iter.headers();
                let raw_rows = iter.take_rows(200);
                let masked_rows: Vec<Vec<String>> = raw_rows.iter().map(|r| r.iter().map(|c| mask_pan_for_ui(c)).collect()).collect();
                let guesses = guess_columns(&headers, &raw_rows);
                self.preview = Some(PreviewFrame { headers: headers.clone(), rows: masked_rows });
                self.guesses = guesses;
                self.mapping.clear();
                self.custom_titles.clear();
                for g in &self.guesses { self.mapping.insert(g.col_index, g.kind); }
                self.status = format!("Analyzed {} columns", headers.len());
            }
            Err(e) => { self.status = format!("Open error: {}", e); self.preview = None; self.guesses.clear(); self.mapping.clear(); self.custom_titles.clear(); }
        }
    }

    fn validate_mapping(&self) -> Result<String> {
        use detector_core::FieldKind::*;
        let mut have_pan = false;
        for (_i, k) in &self.mapping { if *k == Pan { have_pan = true; } if *k == Cvv { anyhow::bail!("Mapping includes CVV — reject by policy"); } }
        if !have_pan { anyhow::bail!("No PAN column detected/selected"); }
        Ok("Mapping looks sane".into())
    }

    fn encrypt_and_upload_full(&mut self) -> Result<()> {
        let path = self.file_path.clone().ok_or_else(|| anyhow::anyhow!("No file chosen"))?;
        self.validate_mapping()?;
        let base = format!("{}:{}", self.cfg.api_base.trim_end_matches('/'), self.cfg.port);
        let client = UploadClient::new(base, self.cfg.site.clone(), self.cfg.username.clone(), self.password.clone());

        let (_kind, mut iter) = open_any(&path)?;
        let headers = iter.headers();
        let mut total = 0usize;
        loop {
            let batch = iter.take_rows(10_000);
            if batch.is_empty() { break; }
            let mut records: Vec<serde_json::Value> = Vec::with_capacity(batch.len());
            for row in &batch {
                let mut obj = serde_json::Map::new();
                for (i, cell) in row.iter().enumerate() {
                    let key = self.custom_titles.get(&i).cloned().unwrap_or_else(|| headers.get(i).cloned().unwrap_or_else(|| format!("col{}", i)));
                    let kind = self.mapping.get(&i).copied().unwrap_or(FieldKind::Unknown);
                    obj.insert(format!("{}::__{:?}", key, kind), serde_json::Value::String(cell.clone()));
                }
                records.push(serde_json::Value::Object(obj));
            }
            let plaintext = serde_json::to_vec(&records)?;
            let key = ContentKey::generate();
            let aad = format!("site={};user={}", self.cfg.site, self.cfg.username).into_bytes();
            let ciphertext = encrypt_chunk(&plaintext, &key, &aad)?;
            client.upload_chunk(ciphertext)?;
            total += records.len();
        }
        self.status = format!("Uploaded {} rows", total);
        Ok(())
    }
}

fn main() -> Result<()> {
    let options = eframe::NativeOptions::default();
    if let Err(e) = eframe::run_native(
        "Rust Encryption Uploader",
        options,
        Box::new(|_| Box::new(MyApp::default())),
    ) { eprintln!("eframe error: {:?}", e); }
    Ok(())
}

# ===== crates/detector_core/Cargo.toml =====
[package]
name = "detector_core"
version = "0.1.0"
edition = "2021"

[dependencies]
anyhow = { workspace = true }
regex = { workspace = true }
serde = { workspace = true }

# ===== crates/detector_core/src/lib.rs =====
use regex::Regex;

#[derive(Clone, Copy, Debug, serde::Serialize, serde::Deserialize, PartialEq, Eq, Hash)]
pub enum FieldKind {
    Pan, FirstName, LastName, ExpMonth, ExpYear, ExpCombined, Cvv, Address1, Address2, City, State, PostalCode, Email, Phone, MerchantRef, Unknown
}

impl FieldKind { pub fn iter_all() -> impl Iterator<Item = FieldKind> {
    [FieldKind::Pan, FieldKind::FirstName, FieldKind::LastName, FieldKind::ExpMonth, FieldKind::ExpYear, FieldKind::ExpCombined, FieldKind::Cvv, FieldKind::Address1, FieldKind::Address2, FieldKind::City, FieldKind::State, FieldKind::PostalCode, FieldKind::Email, FieldKind::Phone, FieldKind::MerchantRef, FieldKind::Unknown].into_iter()
}}

#[derive(Clone, Debug)]
pub struct ColumnGuess { pub col_index: usize, pub header: Option<String>, pub kind: FieldKind, pub score: f32, pub reasons: Vec<String> }

pub fn mask_pan_for_ui(s: &str) -> String {
    let digits: String = s.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() >= 13 && digits.len() <= 19 && luhn_ok(&digits) {
        let last4 = &digits[digits.len().saturating_sub(4)..];
        return format!("############{}", last4);
    }
    s.to_string()
}

pub fn guess_columns(headers: &[String], sample_rows: &[Vec<String>]) -> Vec<ColumnGuess> {
    let mut v = Vec::new();
    for col in 0..headers.len() {
        let col_vals: Vec<&str> = sample_rows.iter().filter_map(|r| r.get(col)).map(|s| s.as_str()).collect();
        let (k, s, r) = classify(headers.get(col).map(|s| s.as_str()), &col_vals);
        v.push(ColumnGuess { col_index: col, header: headers.get(col).cloned(), kind: k, score: s, reasons: r });
    }
    v.sort_by(|a,b| b.score.total_cmp(&a.score));
    v
}

fn classify(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let mut cands: Vec<(FieldKind, f32, Vec<String>)> = vec![
        score_pan(header, vals),
        score_exp_combined(header, vals),
        score_cvv(header, vals),
        score_email(header, vals),
        score_phone(header, vals),
        score_zip(header, vals),
        score_state(header, vals),
        score_first(header, vals),
        score_last(header, vals),
    ];
    cands.sort_by(|a,b| b.1.total_cmp(&a.1));
    let (k,s,r) = cands.remove(0);
    if s > 0.55 { (k,s,r) } else { (FieldKind::Unknown, s, r) }
}

fn score_pan(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let mut hits = 0usize; let mut tested = 0usize;
    for v in vals.iter().take(200) {
        let d: String = v.chars().filter(|c| c.is_ascii_digit()).collect();
        if (13..=19).contains(&d.len()) { tested += 1; if luhn_ok(&d) { hits += 1; } }
    }
    let mut score = if tested>0 { hits as f32 / tested as f32 } else { 0.0 };
    if let Some(h) = header { if contains_any(h, &["pan","card","cc","acct","number"]) { score = (score + 0.6).min(1.0); } }
    (FieldKind::Pan, score, vec![format!("luhn_hits/tested={}/{}", hits, tested)])
}

fn score_exp_combined(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let re = Regex::new(r"(?i)^(0?[1-9]|1[0-2])\s*[/\-]\s*(\d{2}|\d{4})$").unwrap();
    let mut hits = 0usize; let mut n = 0usize; for v in vals.iter().take(200) { n+=1; if re.is_match(v.trim()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["exp","expiry","expiration","valid thru"]) { score = (score + 0.4).min(1.0); } }
    (FieldKind::ExpCombined, score, vec![format!("exp_hits/n={}/{}", hits, n)])
}

fn score_cvv(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let re = Regex::new(r"^[0-9]{3,4}$").unwrap();
    let mut hits = 0usize; let mut n = 0usize; for v in vals.iter().take(200) { n+=1; if re.is_match(v.trim()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["cvv","cvc","cid"]) { score = (score + 0.5).min(1.0); } }
    (FieldKind::Cvv, score, vec![format!("cvv_like/n={}/{}", hits, n)])
}

fn score_email(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let re = Regex::new(r"(?i)^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$").unwrap();
    let mut hits = 0usize; let mut n = 0usize; for v in vals.iter().take(200) { n+=1; if re.is_match(v.trim()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["email"]) { score = (score + 0.6).min(1.0); } }
    (FieldKind::Email, score, vec![format!("email_hits/n={}/{}", hits, n)])
}

fn score_phone(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let re = Regex::new(r"^[+\d][\d\s().-]{6,}$").unwrap();
    let mut hits = 0usize; let mut n = 0usize; for v in vals.iter().take(200) { n+=1; if re.is_match(v.trim()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["phone","mobile","tel"]) { score = (score + 0.4).min(1.0); } }
    (FieldKind::Phone, score, vec![format!("phone_hits/n={}/{}", hits, n)])
}

fn score_zip(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let re = Regex::new(r"^\d{5}(-\d{4})?$").unwrap();
    let mut hits = 0usize; let mut n = 0usize; for v in vals.iter().take(200) { n+=1; if re.is_match(v.trim()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["zip","postal"]) { score = (score + 0.4).min(1.0); } }
    (FieldKind::PostalCode, score, vec![format!("zip_hits/n={}/{}", hits, n)])
}

fn score_state(header: Option<&str>, vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let states = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"];
    let set: std::collections::HashSet<&str> = states.into_iter().collect();
    let mut hits=0usize; let mut n=0usize; for v in vals.iter().take(200) { n+=1; if set.contains(v.trim().to_uppercase().as_str()) { hits+=1; } }
    let mut score = if n>0 { hits as f32 / n as f32 } else {0.0};
    if let Some(h) = header { if contains_any(h, &["state","st"]) { score = (score + 0.3).min(1.0); } }
    (FieldKind::State, score, vec![format!("state_hits/n={}/{}", hits, n)])
}

fn score_first(header: Option<&str>, _vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let mut score = 0.0; if let Some(h) = header { if contains_any(h, &["first","fname","given"]) { score = 0.8; } }
    (FieldKind::FirstName, score, vec!["header".into()])
}

fn score_last(header: Option<&str>, _vals: &[&str]) -> (FieldKind, f32, Vec<String>) {
    let mut score = 0.0; if let Some(h) = header { if contains_any(h, &["last","lname","surname","family"]) { score = 0.8; } }
    (FieldKind::LastName, score, vec!["header".into()])
}

fn contains_any(s: &str, needles: &[&str]) -> bool {
    let low = s.to_lowercase(); needles.iter().any(|n| low.contains(n))
}

pub fn luhn_ok(digits: &str) -> bool {
    if digits.is_empty() { return false; }
    let mut sum = 0; let mut alt = false;
    for ch in digits.chars().rev() {
        if !ch.is_ascii_digit() { return false; }
        let mut n = (ch as u8 - b'0') as i32;
        if alt { n *= 2; if n > 9 { n -= 9; } }
        sum += n; alt = !alt;
    }
    sum % 10 == 0
}

# ===== crates/excel_io/Cargo.toml =====
[package]
name = "excel_io"
version = "0.1.0"
edition = "2021"

[dependencies]
anyhow = { workspace = true }
calamine = { workspace = true }
csv = { workspace = true }

# ===== crates/excel_io/src/lib.rs =====
use anyhow::{Result, anyhow};
use std::io::BufReader;
use std::path::{Path, PathBuf};
use calamine::{self, Reader};

pub enum InputKind { Xlsx, Csv }

pub trait RowIter {
    fn headers(&mut self) -> Vec<String>;
    fn take_rows(&mut self, n: usize) -> Vec<Vec<String>>;
}

pub fn open_any(path: &Path) -> Result<(InputKind, Box<dyn RowIter>)> {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    match ext.as_str() {
        "xlsx" | "xls" => Ok((InputKind::Xlsx, Box::new(XlsxIter::open(path)?))),
        "csv" => Ok((InputKind::Csv, Box::new(CsvIter::open(path)?))),
        _ => Err(anyhow!("Unsupported extension: {}", ext)),
    }
}

struct XlsxIter {
    _path: PathBuf,
    workbook: calamine::Xlsx<BufReader<std::fs::File>>, // exact type from calamine::open_workbook
    sheet_name: String,
    pos: usize,
    headers: Option<Vec<String>>,
}

impl XlsxIter { fn open(path: &Path) -> Result<Self> {
    let mut workbook: calamine::Xlsx<BufReader<std::fs::File>> = calamine::open_workbook(path)?;
    let sheet_name = workbook
        .sheet_names()
        .get(0)
        .cloned()
        .unwrap_or_else(|| "Sheet1".into());
    Ok(Self { _path: path.to_path_buf(), workbook, sheet_name, pos: 0, headers: None })
}}

impl RowIter for XlsxIter {
    fn headers(&mut self) -> Vec<String> {
        if let Some(h) = &self.headers { return h.clone(); }
        let range = match self.workbook.worksheet_range(&self.sheet_name) {
            Some(Ok(r)) => r,
            Some(Err(e)) => { eprintln!("Excel sheet error: {}", e); return vec![]; },
            None => { eprintln!("Missing sheet: {}", self.sheet_name); return vec![]; }
        };
        let mut it = range.rows();
        let first = it.next().unwrap_or(&[]);
        let headers = first.iter().map(|c| c.to_string()).collect::<Vec<_>>();
        self.pos = 1; self.headers = Some(headers.clone()); headers
    }

    fn take_rows(&mut self, n: usize) -> Vec<Vec<String>> {
        let range = match self.workbook.worksheet_range(&self.sheet_name) {
            Some(Ok(r)) => r,
            _ => return vec![],
        };
        let mut out = Vec::new();
        for (i, row) in range.rows().enumerate() {
            if i < self.pos { continue; }
            out.push(row.iter().map(|c| c.to_string()).collect());
            if out.len() >= n { break; }
        }
        self.pos += out.len();
        out
    }
}

struct CsvIter { rdr: csv::Reader<std::fs::File>, headers: Option<Vec<String>> }

impl CsvIter { fn open(path: &Path) -> Result<Self> {
    let f = std::fs::File::open(path)?;
    let rdr = csv::ReaderBuilder::new().flexible(true).from_reader(f);
    Ok(Self { rdr, headers: None })
}}

impl RowIter for CsvIter {
    fn headers(&mut self) -> Vec<String> {
        if let Some(h) = &self.headers { return h.clone(); }
        let h = self
            .rdr
            .headers()
            .map(|h| h.iter().map(|s| s.to_string()).collect())
            .unwrap_or_else(|_| vec![]);
        self.headers = Some(h.clone()); h
    }

    fn take_rows(&mut self, n: usize) -> Vec<Vec<String>> {
        let mut out = Vec::new();
        for rec in self.rdr.records().take(n) {
            if let Ok(r) = rec { out.push(r.iter().map(|s| s.to_string()).collect()); }
        }
        out
    }
}

# ===== crates/crypto/Cargo.toml =====
[package]
name = "crypto"
version = "0.1.0"
edition = "2021"

[dependencies]
anyhow = { workspace = true }
chacha20poly1305 = { workspace = true }
rand_core = { workspace = true }
rand = { workspace = true }
base64 = { workspace = true }

# ===== crates/crypto/src/lib.rs =====
use anyhow::{Result, anyhow};
use chacha20poly1305::{aead::{Aead, KeyInit, Payload}, XChaCha20Poly1305, XNonce};
use rand_core::RngCore;

pub struct ContentKey([u8; 32]);
impl ContentKey { pub fn generate() -> Self { let mut k=[0u8;32]; rand::thread_rng().fill_bytes(&mut k); Self(k) } }

pub fn encrypt_chunk(plaintext: &[u8], key: &ContentKey, aad: &[u8]) -> Result<Vec<u8>> {
    let cipher = XChaCha20Poly1305::new((&key.0).into());
    let mut nonce_bytes = [0u8; 24]; rand::thread_rng().fill_bytes(&mut nonce_bytes);
    let nonce = XNonce::from_slice(&nonce_bytes);
    let ct = cipher
        .encrypt(nonce, Payload { msg: plaintext, aad })
        .map_err(|_| anyhow!("AEAD encryption failed"))?;
    let mut out = nonce_bytes.to_vec();
    out.extend(ct);
    Ok(out)
}

pub fn decrypt_chunk(ciphertext: &[u8], key: &ContentKey, aad: &[u8]) -> Result<Vec<u8>> {
    if ciphertext.len() < 24 { anyhow::bail!("ciphertext too short"); }
    let (nonce_bytes, body) = ciphertext.split_at(24);
    let cipher = XChaCha20Poly1305::new((&key.0).into());
    let nonce = XNonce::from_slice(nonce_bytes);
    let pt = cipher
        .decrypt(nonce, Payload { msg: body, aad })
        .map_err(|_| anyhow!("AEAD decryption failed"))?;
    Ok(pt)
}

# ===== crates/api_client/Cargo.toml =====
[package]
name = "api_client"
version = "0.1.0"
edition = "2021"

[dependencies]
anyhow = { workspace = true }
reqwest = { workspace = true }
uuid = { workspace = true }

# ===== crates/api_client/src/lib.rs =====
use anyhow::Result;
use reqwest::blocking::Client;
use uuid::Uuid;

pub struct UploadClient { base: String, site: String, user: String, pass: String, http: Client }

impl UploadClient {
    pub fn new(base: String, site: String, user: String, pass: String) -> Self {
        let http = Client::builder().brotli(true).gzip(true).deflate(true).build().unwrap();
        Self { base, site, user, pass, http }
    }
    pub fn upload_chunk(&self, ciphertext: Vec<u8>) -> Result<()> {
        let url = format!("{}/v1/uploads", self.base.trim_end_matches('/'));
        let idemp = Uuid::new_v4().to_string();
        let res = self.http.post(url)
            .header("Idempotency-Key", idemp)
            .header("X-Upload-Format", "jsonl")
            .header("X-Enc", "xchacha20poly1305")
            .basic_auth(&self.user, Some(&self.pass))
            .body(ciphertext)
            .send()?;
        if !res.status().is_success() { anyhow::bail!("server status {}", res.status()); }
        Ok(())
    }
}
```
# ===== .env.example =====
UPLOADER_API_BASE=https://example.test
UPLOADER_SITE=mysite
UPLOADER_USERNAME=alice

# ===== README.md =====
# Rust Encryption Uploader (MVP skeleton)

**Dev quickstart**

```bash
rustup default stable
cargo build

# Run the GUI
cargo run -p app_gui
```

**What works now**
- Pick CSV/XLSX → preview masked → column guesses → editable mapping.
- Demo encrypt + POST (to `UPLOADER_API_BASE`).

**What’s next**
- Stream full file rows → chunk & encrypt before serialization.
- Add validation gates (PAN Luhn, no CVV ingestion by policy).
- Replace Basic auth with OAuth/token exchange.
- Persist creds to OS keyring (optional) and redact logs.
- Idempotent, resumable multi-chunk uploads.

