//! Visible-text extraction for `web_fetch` HTML bodies.

pub fn html_to_text(html: &str) -> String {
    const SKIP: &[&str] = &["script", "style", "noscript", "template", "head"];
    const BLOCK: &[&str] = &[
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
    ];
    let mut skip_depth = 0usize;
    let mut parts = String::new();
    let mut link_href: Option<String> = None;
    let mut link_text = String::new();
    let bytes = html.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'<' {
            let rest = &html[i..];
            let closing = rest.starts_with("</");
            let start = if closing { 2 } else { 1 };
            let Some(end_rel) = rest[start..].find('>') else {
                parts.push('<');
                i += 1;
                continue;
            };
            let inside = &rest[start..start + end_rel];
            let tag = inside
                .split(|c: char| c.is_whitespace() || c == '/')
                .next()
                .unwrap_or("")
                .to_ascii_lowercase();
            if SKIP.contains(&tag.as_str()) {
                if closing {
                    skip_depth = skip_depth.saturating_sub(1);
                } else {
                    skip_depth += 1;
                }
            } else if skip_depth == 0 {
                if BLOCK.contains(&tag.as_str()) {
                    parts.push('\n');
                } else if tag == "a" && !closing {
                    link_href = href_from_attrs(inside);
                    link_text.clear();
                } else if tag == "a" && closing {
                    if let Some(href) = link_href.take() {
                        let text = link_text.trim();
                        if !text.is_empty() {
                            parts.push_str(&format!("{text} ({href})"));
                        }
                    }
                    link_text.clear();
                }
            }
            i += start + end_rel + 1;
            continue;
        }
        let next_tag = html[i..].find('<').unwrap_or(html.len() - i);
        let data = &html[i..i + next_tag];
        if skip_depth == 0 {
            if link_href.is_some() {
                link_text.push_str(data);
            }
            parts.push_str(data);
        }
        i += next_tag;
    }
    let lines = parts
        .split('\n')
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "));
    lines
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn href_from_attrs(inside: &str) -> Option<String> {
    let lower = inside.to_ascii_lowercase();
    let idx = lower.find("href=")?;
    let after = inside[idx + 5..].trim_start();
    let quote = after.chars().next()?;
    if quote == '"' || quote == '\'' {
        let rest = &after[1..];
        let end = rest.find(quote)?;
        Some(rest[..end].to_string())
    } else {
        Some(
            after
                .split(|c: char| c.is_whitespace() || c == '>')
                .next()
                .unwrap_or("")
                .to_string(),
        )
    }
}
