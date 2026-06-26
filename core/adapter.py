"""The interface every platform adapter implements.

An adapter FETCHES and NORMALIZES. It does not persist, schedule, or judge virality.
It imports only from `core`, never from another adapter. One adapter == one
`adapters/<platform>/` folder. See docs/INGESTION-CONTRACT.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.schema import GeoTier, PostRecord, Trend, WatchedAccount


class PlatformAdapter(ABC):
    #: short platform key, e.g. "tiktok", "instagram"
    platform: str

    @abstractmethod
    def fetch_account_posts(
        self, account: WatchedAccount, limit: int = 30
    ) -> list[PostRecord]:
        """Recent posts for one Watched Account, newest first.

        Capture maximal metadata; stash the complete original payload in each
        PostRecord.raw. Unknown fields -> None.
        """
        ...

    @abstractmethod
    def fetch_trends(self, geo_tier: GeoTier) -> list[Trend]:
        """Current trends for a Geo Tier. Return [] if the platform exposes no
        trend source (that's fine — not every platform has one)."""
        ...

    def fetch_viral_posts(
        self,
        geo_tier: GeoTier,
        period_days: int = 7,
        hashtags: "list[str] | None" = None,
        locations: "list[str] | None" = None,
    ) -> list[PostRecord]:
        """OPTIONAL discovery (Layer 2): surface viral posts NOT tied to a Watched
        Account, by collecting posts from seeds (trending/education/local-language
        hashtags, locations) and ranking them provisionally. See docs/DISCOVERY.md.

        Distinct from fetch_account_posts (named account) and fetch_trends (patterns).
        Default raises NotImplementedError — implement only if the platform has a
        feasible $0 discovery path. Ranking here is PROVISIONAL, not the final viral
        rule (OPEN-QUESTIONS Q-1).
        """
        raise NotImplementedError(f"{self.platform}: no $0 discovery path implemented")
