use std::fmt::Write;

use serde_json::Value;

pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

pub fn lower(text: &str) -> String {
    text.to_lowercase()
}

pub fn collapse_whitespace(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_space = false;
    for c in text.chars() {
        if is_space(c) {
            if !in_space {
                out.push(' ');
            }
            in_space = true;
        } else {
            out.push(c);
            in_space = false;
        }
    }
    out
}

fn is_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{0b}'
            | '\u{0c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

pub fn splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        if is_line_break(c) {
            lines.push(&text[start..index]);
            let mut end = index + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|(_, next)| *next == '\n') {
                chars.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

pub fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if is_printable(c) => out.push(c),
            c if u32::from(c) <= 0xff => {
                let _ = write!(out, "\\x{:02x}", u32::from(c));
            }
            c if u32::from(c) <= 0xffff => {
                let _ = write!(out, "\\u{:04x}", u32::from(c));
            }
            c => {
                let _ = write!(out, "\\U{:08x}", u32::from(c));
            }
        }
    }
    out.push(quote);
    out
}

fn is_printable(c: char) -> bool {
    if c == ' ' {
        return true;
    }
    !(c.is_control()
        || is_space(c)
        || matches!(u32::from(c), 0xad | 0x200b..=0x200f | 0x2028..=0x202e | 0x2060..=0x206f | 0xfeff | 0xfff9..=0xfffb | 0xe000..=0xf8ff))
}

pub fn repr_list(items: &[String]) -> String {
    let parts: Vec<String> = items.iter().map(|item| repr(item)).collect();
    format!("[{}]", parts.join(", "))
}

pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value > 0.0 {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    let shortest = format!("{value:e}");
    let (mantissa, exponent) = shortest.split_once('e').unwrap_or((&shortest, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    if (-4..16).contains(&exponent) {
        let plain = format!("{value}");
        if plain.contains('.') || plain.contains("inf") {
            plain
        } else {
            format!("{plain}.0")
        }
    } else {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{mantissa}e{sign}{:02}", exponent.abs())
    }
}

pub fn str_of(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                int.to_string()
            } else if let Some(int) = number.as_u64() {
                int.to_string()
            } else {
                float_repr(number.as_f64().unwrap_or_default())
            }
        }
        Value::String(text) => text.clone(),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(repr_of).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(key, value)| format!("{}: {}", repr(key), repr_of(value)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn repr_of(value: &Value) -> String {
    match value {
        Value::String(text) => repr(text),
        other => str_of(other),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn repr_matches_python_quoting() {
        assert_eq!(repr("yes"), "'yes'");
        assert_eq!(repr("it's"), "\"it's\"");
        assert_eq!(repr("both ' and \""), "'both \\' and \"'");
        assert_eq!(repr("tab\tnew\nline\\"), "'tab\\tnew\\nline\\\\'");
        assert_eq!(repr("ünïcødé ✓"), "'ünïcødé ✓'");
        assert_eq!(repr("\u{7}"), "'\\x07'");
        assert_eq!(repr_list(&["a".into(), "b'c".into()]), "['a', \"b'c\"]");
        assert_eq!(repr_list(&[]), "[]");
    }

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(
            splitlines("a\nb\r\nc\rd\u{2028}e"),
            vec!["a", "b", "c", "d", "e"]
        );
        assert_eq!(splitlines("a\n"), vec!["a"]);
        assert_eq!(splitlines("\n\nx"), vec!["", "", "x"]);
        assert_eq!(splitlines(""), Vec::<&str>::new());
    }

    #[test]
    fn str_of_matches_python_str() {
        assert_eq!(str_of(&json!(4)), "4");
        assert_eq!(str_of(&json!(1.0)), "1.0");
        assert_eq!(str_of(&json!(0.25)), "0.25");
        assert_eq!(str_of(&json!(1e-5)), "1e-05");
        assert_eq!(str_of(&json!(1e16)), "1e+16");
        assert_eq!(str_of(&json!(true)), "True");
        assert_eq!(str_of(&json!([1, "a"])), "[1, 'a']");
        assert_eq!(str_of(&json!({"k": null})), "{'k': None}");
    }

    #[test]
    fn whitespace_includes_python_extras() {
        assert!(is_space('\u{1c}'));
        assert_eq!(strip("\u{1f} x \u{a0}"), "x");
        assert_eq!(collapse_whitespace("a \t\n b"), "a b");
    }
}
