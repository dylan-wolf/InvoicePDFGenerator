```rust
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
        Self {
            site: String::new(),
            username: String::new(),
            api_base: String::new(),
            port: 443,
        }
    }
}

struct PreviewFrame {
    headers: Vec<String>,
    rows: Vec<Vec<String>>, // masked for UI
}

struct MyApp {
    cfg: AppCfg,
    password: String, // not persisted
    file_path: Option<PathBuf>,
    preview: Option<PreviewFrame>,
    guesses: Vec<ColumnGuess>,
    mapping: HashMap<usize, FieldKind>,
    status: String,
    // NEW: Sites from .env and a tiny "wizardish" next-step
    sites: Vec<String>,
}

impl Default for MyApp {
    fn default() -> Self {
        // Load env if present
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
            cfg.port = std::env::var("UPLOADER_PORT")
                .ok()
                .and_then(|s| s.parse::<u16>().ok())
                .unwrap_or(443);
        }

        // NEW: collect sites list from .env (comma-separated)
        let sites = std::env::var("UPLOADER_SITES")
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();

        Self {
            cfg,
            password: String::new(),
            file_path: None,
            preview: None,
            guesses: vec![],
            mapping: HashMap::new(),
            status: String::new(),
            sites,
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

            // Site: dropdown from .env if available; otherwise free text
            ui.horizontal(|ui| {
                ui.label("Site");
                if self.sites.is_empty() {
                    ui.text_edit_singleline(&mut self.cfg.site);
                } else {
                    egui::ComboBox::from_id_source("site_combo")
                        .selected_text(if self.cfg.site.is_empty() {
                            "Select site".to_string()
                        } else {
                            self.cfg.site.clone()
                        })
                        .show_ui(ui, |ui| {
                            for s in &self.sites {
                                let selected = self.cfg.site == *s;
                                if ui.selectable_label(selected, s).clicked() {
                                    self.cfg.site = s.clone();
                                }
                            }
                        });
                }
            });

            ui.horizontal(|ui| { ui.label("API Base"); ui.text_edit_singleline(&mut self.cfg.api_base); });

            // NEW: Port field
            ui.horizontal(|ui| {
                ui.label("Port");
                ui.add(egui::DragValue::new(&mut self.cfg.port).clamp_range(1..=65535));
            });

            ui.horizontal(|ui| { ui.label("Username"); ui.text_edit_singleline(&mut self.cfg.username); });

            ui.horizontal(|ui| { ui.label("Password");
                let mut pw = egui::text::TextEdit::singleline(&mut self.password);
                pw = pw.password(true);
                ui.add(pw);
            });

            if ui.button("Save non-secrets").clicked() {
                if let Err(e) = confy::store("rust-encryption-uploader", None::<&str>, &self.cfg) {
                    self.status = format!("Save error: {}", e);
                } else {
                    self.status = "Saved".into();
                }
            }

            ui.separator();

            // "Next" button: if no file chosen, prompt for file; otherwise nudge user forward.
            ui.horizontal(|ui| {
                if ui.button("Next →").clicked() {
                    if self.file_path.is_none() {
                        if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() {
                            self.load_preview(path);
                        }
                    } else {
                        self.status = "Proceed to Preview & Mapping (center panel).".into();
                    }
                }
                if ui.button("Pick file…").clicked() {
                    if let Some(path) = rfd::FileDialog::new().add_filter("Data", &["xlsx","xls","csv"]).pick_file() {
                        self.load_preview(path);
                    }
                }
            });

            if let Some(p) = &self.file_path { ui.label(format!("File: {}", p.display())); }

            ui.separator();
            ui.label(RichText::new(&self.status).monospace());
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("Preview & Mapping");
            if let Some(prev) = &self.preview {
                ScrollArea::both().auto_shrink([false; 2]).show(ui, |ui| {
                    egui::Grid::new("preview_grid").striped(true).show(ui, |ui| {
                        // headers
                        for h in &prev.headers { ui.label(RichText::new(h)); }
                        ui.end_row();
                        // rows (first 50)
                        for row in prev.rows.iter().take(50) {
                            for cell in row { ui.label(cell); }
                            ui.end_row();
                        }
                    });
                });
                ui.separator();
                ui.heading("Inferred mapping (editable)");
                for (idx, guess) in self.guesses.iter().enumerate() { 
                    ui.horizontal(|ui| {
                        ui.label(format!("Col {}: {}", guess.col_index, guess.header.clone().unwrap_or_else(|| "(no header)".into())));
                        let mut changed = false;
                        egui::ComboBox::from_id_source(format!("kind_{}", idx))
                            .selected_text(format!("{:?}", self.mapping.get(&guess.col_index).copied().unwrap_or(guess.kind)))
                            .show_ui(ui, |ui| {
                                for k in FieldKind::iter_all() {
                                    if ui.selectable_label(false, format!("{:?}", k)).clicked() {
                                        self.mapping.insert(guess.col_index, k);
                                        changed = true;
                                    }
                                }
                            });
                        if changed { self.status.clear(); }
                        if ui.small_button("why?").clicked() {
                            self.status = format!("Reasons for col {} → {:?}: {}", guess.col_index, guess.kind, guess.reasons.join(" | "));
                        }
                    });
                }

                ui.separator();
                // Action row
                let can_upload = self.file_path.is_some() && !self.password.is_empty();
                ui.horizontal(|ui| {
                    if ui.button("Validate mapping").clicked() {
                        match self.validate_mapping() { Ok(msg) => self.status = msg, Err(e) => self.status = format!("Validation failed: {}", e) }
                    }
                    let mut btn = egui::Button::new("Encrypt & Upload");
                    if !can_upload { btn = btn.sense(egui::Sense::hover()); }
                    if ui.add(btn).clicked() {
                        match self.encrypt_and_upload_full() { Ok(_) => self.status = "Upload complete".into(), Err(e) => self.status = format!("Upload error: {}", e) }
                    }
                    if !can_upload {
                        ui.label(egui::RichText::new("← Enter password and pick a file to enable").italics());
                    }
                });
            } else {
                ui.label("Click “Next →” or “Pick file…” after filling in connection details.");
            }
        });
    }
}

impl MyApp {
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
                for g in &self.guesses { self.mapping.insert(g.col_index, g.kind); }
                self.status = format!("Analyzed {} columns", headers.len());
            }
            Err(e) => { self.status = format!("Open error: {}", e); self.preview = None; self.guesses.clear(); self.mapping.clear(); }
        }
    }

    fn validate_mapping(&self) -> Result<String> {
        use detector_core::FieldKind::*;
        let mut have_pan = false;
        let mut have_name = false;
        for (_i, k) in &self.mapping {
            match k {
                Pan => have_pan = true,
                FirstName | LastName => have_name = true,
                Cvv => anyhow::bail!("Mapping includes CVV — reject by policy"),
                _ => {}
            }
        }
        if !have_pan { anyhow::bail!("No PAN column detected/selected"); }
        if !have_name { /* optional but recommended */ }
        Ok("Mapping looks sane".into())
    }

    fn encrypt_and_upload_full(&mut self) -> Result<()> {
        let path = self.file_path.clone().ok_or_else(|| anyhow::anyhow!("No file chosen"))?;
        self.validate_mapping()?;

        // Build base:port
        let api_base = format!("{}:{}", self.cfg.api_base.trim_end_matches('/'), self.cfg.port);
        let client = UploadClient::new(api_base, self.cfg.site.clone(), self.cfg.username.clone(), self.password.clone());

        // Re-open iterator and stream rows in batches
        let (_kind, mut iter) = open_any(&path)?;
        let headers = iter.headers();
        let mut total = 0usize;
        loop {
            let batch = iter.take_rows(10_000);
            if batch.is_empty() { break; }
            // Build JSONL-like vector of objects (masked only for display earlier; here we send raw cells)
            let mut records: Vec<serde_json::Value> = Vec::with_capacity(batch.len());
            for row in &batch {
                let mut obj = serde_json::Map::new();
                for (i, cell) in row.iter().enumerate() {
                    let key = headers.get(i).cloned().unwrap_or_else(|| format!("col{}", i));
                    // Use mapping tag so server knows semantics per column
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
    ) {
        eprintln!("eframe error: {:?}", e);
    }
    Ok(())
}
```
