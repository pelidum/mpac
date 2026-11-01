use std::sync::LazyLock;

use infer::Infer;
use mpac_proto::FileModality;

pub const SIGNATURE_BYTES: usize = 8192;
const EBML_MAGIC: &[u8] = &[0x1a, 0x45, 0xdf, 0xa3];

static SNIFFER: LazyLock<Infer> = LazyLock::new(|| {
    let mut sniffer = Infer::new();
    sniffer.add("video/webm", "webm", |buf| {
        ebml_doctype(buf, b"\x42\x82\x84webm")
    });
    sniffer.add("video/x-matroska", "mkv", |buf| {
        ebml_doctype(buf, b"\x42\x82\x88matroska")
    });
    sniffer
});

fn ebml_doctype(buf: &[u8], doctype: &[u8]) -> bool {
    let head = &buf[..buf.len().min(SIGNATURE_BYTES)];
    head.starts_with(EBML_MAGIC) && head.windows(doctype.len()).any(|w| w == doctype)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sniffed {
    pub modality: FileModality,
    pub mime: String,
    pub extension: String,
}

pub fn sniff(name: &str, content: &[u8]) -> Option<Sniffed> {
    sniff_head(
        name,
        &content[..content.len().min(SIGNATURE_BYTES)],
        std::str::from_utf8(content).is_ok(),
    )
}

pub fn sniff_head(name: &str, head: &[u8], utf8: bool) -> Option<Sniffed> {
    let Some(kind) = SNIFFER.get(head) else {
        if !utf8 {
            return None;
        }
        let extension = name
            .rsplit_once('.')
            .map(|(_, ext)| ext)
            .filter(|ext| !ext.is_empty())
            .unwrap_or("txt");
        return Some(Sniffed {
            modality: FileModality::Text,
            mime: "text/plain".into(),
            extension: extension.to_owned(),
        });
    };
    let modality = match kind.mime_type().split('/').next() {
        Some("audio") => FileModality::Audio,
        Some("image") => FileModality::Image,
        Some("video") => FileModality::Video,
        _ => return None,
    };
    Some(Sniffed {
        modality,
        mime: python_mime(kind.mime_type()).to_owned(),
        extension: kind.extension().to_owned(),
    })
}

fn python_mime(mime: &str) -> &str {
    match mime {
        "audio/m4a" => "audio/mp4",
        other => other,
    }
}

#[derive(Debug, Default)]
pub struct Utf8Validator {
    pending: Vec<u8>,
    invalid: bool,
}

impl Utf8Validator {
    pub fn feed(&mut self, chunk: &[u8]) {
        if self.invalid {
            return;
        }
        let mut buf = std::mem::take(&mut self.pending);
        buf.extend_from_slice(chunk);
        if let Err(error) = std::str::from_utf8(&buf) {
            match error.error_len() {
                Some(_) => self.invalid = true,
                None => self.pending = buf[error.valid_up_to()..].to_vec(),
            }
        }
    }

    pub fn is_valid(&self) -> bool {
        !self.invalid && self.pending.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_fallback_uses_name_extension_or_txt() {
        let notes = sniff("notes.md", "ünïcødé".as_bytes()).unwrap();
        assert_eq!(notes.modality, FileModality::Text);
        assert_eq!(notes.extension, "md");
        assert_eq!(sniff("README", b"hello").unwrap().extension, "txt");
    }

    #[test]
    fn rejects_non_media_and_non_utf8() {
        assert_eq!(sniff("doc.pdf", b"%PDF-1.4\n"), None);
        assert_eq!(sniff("blob.bin", &[0x80, 0xFF, 0xFE]), None);
    }

    #[test]
    fn short_matroska_headers_match_python_filetype() {
        let webm = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm";
        let mkv = b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska";

        assert_eq!(sniff("a.webm", webm).unwrap().mime, "video/webm");
        assert_eq!(sniff("a.mkv", mkv).unwrap().extension, "mkv");
    }

    #[test]
    fn png_is_image() {
        let png = [
            0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0x0D, b'I', b'H', b'D', b'R',
        ];
        let sniffed = sniff("pixel.bin", &png).unwrap();
        assert_eq!(sniffed.modality, FileModality::Image);
        assert_eq!(sniffed.mime, "image/png");
        assert_eq!(sniffed.extension, "png");
    }

    #[test]
    fn streamed_utf8_matches_whole_buffer_validation() {
        let text = "aé€😀".repeat(50);
        for split in 1..text.len() {
            let mut validator = Utf8Validator::default();
            for chunk in text.as_bytes().chunks(split) {
                validator.feed(chunk);
            }
            assert!(validator.is_valid(), "split {split}");
        }
        let mut truncated = Utf8Validator::default();
        truncated.feed(&"€".as_bytes()[..2]);
        assert!(!truncated.is_valid());
        let mut broken = Utf8Validator::default();
        broken.feed(b"ok");
        broken.feed(&[0xFF]);
        broken.feed(b"more");
        assert!(!broken.is_valid());
    }
}
