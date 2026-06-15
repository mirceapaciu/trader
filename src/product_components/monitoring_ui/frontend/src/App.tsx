import { useState } from "react";

import { NewsFetcherTab } from "./newsFetcher/NewsFetcherTab";
import { ThesisBuilderTab } from "./thesisBuilder/ThesisBuilderTab";

type DomainTab = "news-fetcher" | "thesis-builder";

const DOMAIN_TABS: Array<{ id: DomainTab; label: string }> = [
  { id: "news-fetcher", label: "NewsFetcher" },
  { id: "thesis-builder", label: "ThesisBuilder" }
];

export function App() {
  const [activeTab, setActiveTab] = useState<DomainTab>("news-fetcher");

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
      {activeTab === "news-fetcher" ? <NewsFetcherTab /> : <ThesisBuilderTab />}
    </>
  );
}
