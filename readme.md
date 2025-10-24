'''rust

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
        Self {
            cfg,
            password: String::new(),
            file_path: None,
            preview: None,
            guesses: vec![],
            mapping: HashMap::new(),
            custom_titles: HashMap::new(),
            status: String::new(),
            step: UiStep::Connect,
            sites
        }
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
                    // Move to mapping step; inference already done during load_preview
                    if self.file_path.is_some() { self.step = UiStep::Map; }
                    else if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() {
                        self.load_preview(path);
                        self.step = UiStep::Map;
                    }
                }
                if ui.button("Pick file…").clicked() {
                    if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() {
                        // NOTE: stay on Connect step to allow pre-Next verification
                        self.load_preview(path);
                        self.step = UiStep::Connect;
                        self.status = "Preview loaded. Verify file below, then click Next →".into();
                    }
                }
            });
            if let Some(p) = &self.file_path { ui.label(format!("File: {}", p.display())); }
            if let Some(prev) = &self.preview {
                ui.label(format!("Analyzed {} columns", prev.headers.len()));
            }
            ui.separator();
            ui.label(RichText::new(&self.status).monospace());
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            match self.step {
                UiStep::Connect => self.connect_preview(ui), // NEW: show preview before Next
                UiStep::Map => self.map_view(ui),
            }
        });
    }
}

impl MyApp {
    /// NEW: lightweight preview shown on the Connect step after picking a file.
    fn connect_preview(&mut self, ui: &mut egui::Ui) {
        ui.heading("File Preview");
        if let Some(prev) = &self.preview {
            ui.label("Verify this is the correct file. If it looks good, click Next → to assign titles.");
            ScrollArea::both().id_source("preview_scroll_connect").auto_shrink([false; 2]).show(ui, |ui| {
                egui::Grid::new("preview_grid_connect").striped(true).show(ui, |ui| {
                    for h in &prev.headers { ui.label(RichText::new(h)); }
                    ui.end_row();
                    for row in prev.rows.iter().take(5) { // show 3–5 rows
                        for cell in row { ui.label(cell); }
                        ui.end_row();
                    }
                });
            });
        } else {
            ui.label("Pick a file to see a quick preview, then click Next →.");
        }
    }

    fn map_view(&mut self, ui: &mut egui::Ui) {
        ui.heading("Preview & Assign Titles");
        if let Some(prev) = &self.preview {
            ui.spacing_mut().item_spacing.y = 6.0;
            ui.label("Assumed titles (editable below)");
            ScrollArea::horizontal().id_source("assumed_titles_scroll").show(ui, |ui| {
                egui::Grid::new("assumed_titles_grid").striped(true).show(ui, |ui| {
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
            ScrollArea::both().id_source("preview_scroll_map").auto_shrink([false; 2]).show(ui, |ui| {
                egui::Grid::new("preview_grid_map").striped(true).show(ui, |ui| {
                    for h in &prev.headers { ui.label(RichText::new(h)); }
                    ui.end_row();
                    for row in prev.rows.iter().take(5) {
                        for cell in row { ui.label(cell); }
                        ui.end_row();
                    }
                });
            });

            ui.separator();
            ui.heading("Assign titles per column");
            for (idx, header) in prev.headers.iter().enumerate() {
                ui.push_id(idx, |ui| { // ensure unique widget ids within the row
                    ui.horizontal(|ui| {
                        ui.label(format!("Col {}: {}", idx, if header.is_empty() { "(no header)" } else { header }));

                        // Dropdown of known kinds
                        let current_kind = self.mapping.get(&idx).copied().unwrap_or(FieldKind::Unknown);
                        egui::ComboBox::from_id_source("kind_combo")
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
'''

