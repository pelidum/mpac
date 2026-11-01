use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

use bytes::Bytes;

use crate::{BlobError, BlobStore};

pub const MAX_LOAD_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Debug)]
struct Entries {
    blobs: HashMap<String, Bytes>,
    recency: VecDeque<String>,
    bytes: usize,
}

#[derive(Debug)]
pub struct BlobCache {
    capacity: usize,
    entries: Mutex<Entries>,
    loading: tokio::sync::Mutex<()>,
}

impl BlobCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity,
            loading: tokio::sync::Mutex::new(()),
            entries: Mutex::new(Entries {
                blobs: HashMap::new(),
                recency: VecDeque::new(),
                bytes: 0,
            }),
        }
    }

    pub fn get(&self, key: &str) -> Option<Bytes> {
        let mut entries = self.entries.lock().ok()?;
        let blob = entries.blobs.get(key).cloned()?;
        entries.recency.retain(|k| k != key);
        entries.recency.push_back(key.to_owned());
        Some(blob)
    }

    pub fn insert(&self, key: &str, blob: Bytes) {
        if blob.len() > self.capacity {
            return;
        }
        let Ok(mut entries) = self.entries.lock() else {
            return;
        };
        let size = blob.len();
        if let Some(previous) = entries.blobs.insert(key.to_owned(), blob) {
            entries.bytes -= previous.len();
            entries.recency.retain(|k| k != key);
        }
        entries.bytes += size;
        entries.recency.push_back(key.to_owned());
        while entries.bytes > self.capacity {
            let Some(oldest) = entries.recency.pop_front() else {
                break;
            };
            if let Some(evicted) = entries.blobs.remove(&oldest) {
                entries.bytes -= evicted.len();
            }
        }
    }

    pub async fn get_or_load(&self, store: &BlobStore, key: &str) -> Result<Bytes, BlobError> {
        if let Some(blob) = self.get(key) {
            return Ok(blob);
        }
        let _loading = self.loading.lock().await;
        if let Some(blob) = self.get(key) {
            return Ok(blob);
        }
        let blob = store.get_limited(key, MAX_LOAD_BYTES).await?;
        self.insert(key, blob.clone());
        Ok(blob)
    }

    pub fn len_bytes(&self) -> usize {
        self.entries.lock().map_or(0, |entries| entries.bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn simultaneous_loads_share_cached_bytes() {
        let store = BlobStore::in_memory();
        store
            .put("blobs/x", Bytes::from(vec![42; 1024]))
            .await
            .unwrap();
        let cache = BlobCache::new(2048);
        let (first, second) = tokio::join!(
            cache.get_or_load(&store, "blobs/x"),
            cache.get_or_load(&store, "blobs/x")
        );
        assert_eq!(first.unwrap().as_ptr(), second.unwrap().as_ptr());
    }

    #[test]
    fn evicts_least_recently_used_until_under_capacity() {
        let cache = BlobCache::new(10);
        cache.insert("a", Bytes::from_static(b"aaaa"));
        cache.insert("b", Bytes::from_static(b"bbbb"));
        assert!(cache.get("a").is_some());

        cache.insert("c", Bytes::from_static(b"cccc"));

        assert!(cache.get("a").is_some());
        assert!(cache.get("b").is_none());
        assert!(cache.get("c").is_some());
        assert_eq!(cache.len_bytes(), 8);
    }

    #[test]
    fn oversized_blobs_are_not_cached() {
        let cache = BlobCache::new(3);
        cache.insert("big", Bytes::from_static(b"four"));

        assert!(cache.get("big").is_none());
        assert_eq!(cache.len_bytes(), 0);
    }

    #[test]
    fn reinserting_replaces_the_previous_size() {
        let cache = BlobCache::new(10);
        cache.insert("a", Bytes::from_static(b"aaaa"));
        cache.insert("a", Bytes::from_static(b"aa"));

        assert_eq!(cache.len_bytes(), 2);
    }
}
