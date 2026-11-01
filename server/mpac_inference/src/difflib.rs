use std::collections::HashMap;

struct Matcher<'b> {
    b: &'b [char],
    b2j: HashMap<char, Vec<usize>>,
    full_b_count: HashMap<char, usize>,
}

impl<'b> Matcher<'b> {
    fn new(b: &'b [char]) -> Self {
        let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
        for (index, c) in b.iter().enumerate() {
            b2j.entry(*c).or_default().push(index);
        }
        if b.len() >= 200 {
            let limit = b.len() / 100 + 1;
            b2j.retain(|_, indices| indices.len() <= limit);
        }
        let mut full_b_count = HashMap::new();
        for c in b {
            *full_b_count.entry(*c).or_insert(0) += 1;
        }
        Self {
            b,
            b2j,
            full_b_count,
        }
    }

    fn longest_match(
        &self,
        a: &[char],
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (mut best_i, mut best_j, mut best_size) = (alo, blo, 0);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut next: HashMap<usize, usize> = HashMap::new();
            if let Some(indices) = self.b2j.get(c) {
                for &j in indices {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j
                        .checked_sub(1)
                        .and_then(|p| j2len.get(&p))
                        .copied()
                        .unwrap_or(0)
                        + 1;
                    next.insert(j, k);
                    if k > best_size {
                        best_i = i + 1 - k;
                        best_j = j + 1 - k;
                        best_size = k;
                    }
                }
            }
            j2len = next;
        }
        let b = self.b;
        while best_i > alo && best_j > blo && a[best_i - 1] == b[best_j - 1] {
            best_i -= 1;
            best_j -= 1;
            best_size += 1;
        }
        while best_i + best_size < ahi
            && best_j + best_size < bhi
            && a[best_i + best_size] == b[best_j + best_size]
        {
            best_size += 1;
        }
        (best_i, best_j, best_size)
    }

    fn matches(&self, a: &[char]) -> usize {
        let mut queue = vec![(0, a.len(), 0, self.b.len())];
        let mut blocks = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.longest_match(a, alo, ahi, blo, bhi);
            if k > 0 {
                blocks.push((i, j, k));
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        blocks.iter().map(|(_, _, k)| k).sum()
    }

    fn quick_ratio(&self, a: &[char]) -> f64 {
        let mut available: HashMap<char, isize> = HashMap::new();
        let mut matches = 0_usize;
        for c in a {
            let count = available.entry(*c).or_insert_with(|| {
                isize::try_from(self.full_b_count.get(c).copied().unwrap_or(0)).unwrap_or(0)
            });
            if *count > 0 {
                matches += 1;
            }
            *count -= 1;
        }
        ratio(matches, a.len() + self.b.len())
    }
}

#[allow(clippy::cast_precision_loss)]
fn ratio(matches: usize, length: usize) -> f64 {
    if length == 0 {
        1.0
    } else {
        2.0 * matches as f64 / length as f64
    }
}

pub fn similarity(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let matcher = Matcher::new(&b);
    ratio(matcher.matches(&a), a.len() + b.len())
}

#[allow(clippy::float_cmp)]
pub fn closest_match<'p>(
    word: &str,
    possibilities: &'p [String],
    cutoff: f64,
) -> Option<&'p String> {
    let b: Vec<char> = word.chars().collect();
    let matcher = Matcher::new(&b);
    let mut best: Option<(f64, &String)> = None;
    for candidate in possibilities {
        let a: Vec<char> = candidate.chars().collect();
        let total = a.len() + b.len();
        if ratio(a.len().min(b.len()), total) < cutoff || matcher.quick_ratio(&a) < cutoff {
            continue;
        }
        let score = ratio(matcher.matches(&a), total);
        if score < cutoff {
            continue;
        }
        let better = match best {
            None => true,
            Some((best_score, best_text)) => {
                score > best_score || (score == best_score && candidate > best_text)
            }
        };
        if better {
            best = Some((score, candidate));
        }
    }
    best.map(|(_, text)| text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn similarity_matches_python_ratio() {
        assert!((similarity("abcd", "bcde") - 0.75).abs() < 1e-12);
        assert!((similarity("positiv", "positive") - 14.0 / 15.0).abs() < 1e-12);
        assert!((similarity("", "") - 1.0).abs() < 1e-12);
    }

    #[test]
    fn closest_match_breaks_ties_by_larger_string() {
        let possibilities = vec!["ab".to_owned(), "ba".to_owned()];

        assert_eq!(
            closest_match("a", &possibilities, 0.6).map(String::as_str),
            Some("ba")
        );
        assert_eq!(closest_match("zz", &possibilities, 0.6), None);
    }
}
