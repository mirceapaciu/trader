import { useState } from "react";

import { BacktesterTab } from "./backtester/BacktesterTab";
import { NewsFetcherTab } from "./newsFetcher/NewsFetcherTab";
import { OverviewTab } from "./overview/OverviewTab";
import { ThesisBuilderTab } from "./thesisBuilder/ThesisBuilderTab";
import { WatchlistTab } from "./watchlist/WatchlistTab";

type DomainTab = "overview" | "news-fetcher" | "thesis-builder" | "watchlist" | "backtester";

const DOMAIN_TABS: Array<{ id: DomainTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "news-fetcher", label: "NewsFetcher" },
  { id: "thesis-builder", label: "ThesisBuilder" },
  { id: "watchlist", label: "Watchlist" },
  { id: "backtester", label: "Backtester" }
];

export function App() {
  const [activeTab, setActiveTab] = useState<DomainTab>("overview");

  return (
    <>
      <nav className="domain-tabs" aria-label="Monitoring domains">
        {DOMAIN_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={activeTab === tab.id ? "domain-tab active" : "domain-tab"}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      {activeTab === "overview" ? <OverviewTab /> : null}
      {activeTab === "news-fetcher" ? <NewsFetcherTab /> : null}
      {activeTab === "thesis-builder" ? <ThesisBuilderTab /> : null}
      {activeTab === "watchlist" ? <WatchlistTab /> : null}
      {activeTab === "backtester" ? <BacktesterTab /> : null}
    </>
  );
}
