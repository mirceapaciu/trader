import { fireEvent, render, screen } from "@testing-library/react";

import { App } from "./App";

vi.mock("./newsFetcher/NewsFetcherTab", () => ({
  NewsFetcherTab: () => <div>NewsFetcher content</div>
}));

vi.mock("./thesisBuilder/ThesisBuilderTab", () => ({
  ThesisBuilderTab: () => <div>ThesisBuilder content</div>
}));

vi.mock("./watchlist/WatchlistTab", () => ({
  WatchlistTab: () => <div>Watchlist content</div>
}));

describe("App", () => {
  it("switches to the Watchlist tab", () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Watchlist" }));

    expect(screen.getByText("Watchlist content")).toBeInTheDocument();
  });
});
