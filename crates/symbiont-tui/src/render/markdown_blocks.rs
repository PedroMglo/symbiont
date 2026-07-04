#[derive(Debug, Clone, Eq, PartialEq)]
pub enum MarkdownBlock {
    Text(String),
    Code {
        language: Option<String>,
        code: String,
    },
    Shell {
        language: Option<String>,
        script: String,
    },
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum InlineSpan {
    Text(String),
    Code(String),
}

pub fn parse_markdown_blocks(text: &str) -> Vec<MarkdownBlock> {
    let mut blocks = Vec::new();
    let mut text_buffer = Vec::new();
    let mut fence_language: Option<String> = None;
    let mut fence_buffer = Vec::new();

    for line in text.lines() {
        if let Some(language) = line.trim_start().strip_prefix("```") {
            if let Some(open_language) = fence_language.take() {
                push_text(&mut blocks, &mut text_buffer);
                let body = fence_buffer.join("\n");
                fence_buffer.clear();
                if is_shell_language(&open_language) {
                    blocks.push(MarkdownBlock::Shell {
                        language: clean_language(&open_language),
                        script: body,
                    });
                } else {
                    blocks.push(MarkdownBlock::Code {
                        language: clean_language(&open_language),
                        code: body,
                    });
                }
            } else {
                push_text(&mut blocks, &mut text_buffer);
                fence_language = Some(language.trim().to_string());
            }
            continue;
        }

        if fence_language.is_some() {
            fence_buffer.push(line.to_string());
        } else {
            text_buffer.push(line.to_string());
        }
    }

    if let Some(open_language) = fence_language {
        push_text(&mut blocks, &mut text_buffer);
        let body = fence_buffer.join("\n");
        if is_shell_language(&open_language) {
            blocks.push(MarkdownBlock::Shell {
                language: clean_language(&open_language),
                script: body,
            });
        } else {
            blocks.push(MarkdownBlock::Code {
                language: clean_language(&open_language),
                code: body,
            });
        }
    }
    push_text(&mut blocks, &mut text_buffer);

    if blocks.is_empty() && !text.trim().is_empty() {
        blocks.push(MarkdownBlock::Text(text.to_string()));
    }
    blocks
}

pub fn split_inline_code(line: &str) -> Vec<InlineSpan> {
    let mut spans = Vec::new();
    let mut buffer = String::new();
    let mut code = false;

    for part in line.split('`') {
        if code {
            spans.push(InlineSpan::Code(part.to_string()));
        } else if !part.is_empty() {
            buffer.push_str(part);
            spans.push(InlineSpan::Text(std::mem::take(&mut buffer)));
        }
        code = !code;
    }

    if spans.is_empty() {
        spans.push(InlineSpan::Text(line.to_string()));
    }
    spans
}

fn push_text(blocks: &mut Vec<MarkdownBlock>, buffer: &mut Vec<String>) {
    let text = trim_joined(buffer);
    buffer.clear();
    if !text.is_empty() {
        blocks.push(MarkdownBlock::Text(text));
    }
}

fn trim_joined(lines: &[String]) -> String {
    lines.join("\n").trim_matches('\n').to_string()
}

fn clean_language(language: &str) -> Option<String> {
    let clean = language
        .split_whitespace()
        .next()
        .unwrap_or("")
        .trim()
        .trim_start_matches('{')
        .trim_end_matches('}')
        .to_ascii_lowercase();
    if clean.is_empty() {
        None
    } else {
        Some(clean)
    }
}

fn is_shell_language(language: &str) -> bool {
    matches!(
        clean_language(language).as_deref(),
        Some("bash" | "sh" | "shell" | "zsh" | "fish" | "console" | "terminal")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_text_code_and_shell_blocks() {
        let blocks = parse_markdown_blocks("hi\n```rust\nfn main() {}\n```\n```bash\npytest -q\n```");
        assert_eq!(
            blocks,
            vec![
                MarkdownBlock::Text("hi".to_string()),
                MarkdownBlock::Code {
                    language: Some("rust".to_string()),
                    code: "fn main() {}".to_string(),
                },
                MarkdownBlock::Shell {
                    language: Some("bash".to_string()),
                    script: "pytest -q".to_string(),
                },
            ]
        );
    }

    #[test]
    fn keeps_unclosed_fence_as_card() {
        let blocks = parse_markdown_blocks("```python\nprint('x')");
        assert_eq!(
            blocks,
            vec![MarkdownBlock::Code {
                language: Some("python".to_string()),
                code: "print('x')".to_string(),
            }]
        );
    }
}
