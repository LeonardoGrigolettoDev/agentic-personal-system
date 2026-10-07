package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"aios/decision/internal/decide"
	"github.com/redis/go-redis/v9"
)

const cacheTTL = time.Hour

// Cache stores final answers keyed by decide.CacheKey. Every failure is the caller's to ignore.
type Cache struct{ rdb *redis.Client }

func NewCache(addr, password string, db int) *Cache {
	return &Cache{rdb: redis.NewClient(&redis.Options{
		Addr:         addr,
		Password:     password,
		DB:           db,
		DialTimeout:  time.Second,
		ReadTimeout:  500 * time.Millisecond,
		WriteTimeout: 500 * time.Millisecond,
		MaxRetries:   1,
	})}
}

func (c *Cache) Close() error { return c.rdb.Close() }

// Get returns (nil, nil) on a miss.
func (c *Cache) Get(ctx context.Context, key string) ([]decide.Answer, error) {
	b, err := c.rdb.Get(ctx, key).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var answers []decide.Answer
	return answers, json.Unmarshal(b, &answers)
}

func (c *Cache) Set(ctx context.Context, key string, answers []decide.Answer) error {
	b, err := json.Marshal(answers)
	if err != nil {
		return err
	}
	return c.rdb.Set(ctx, key, b, cacheTTL).Err()
}
