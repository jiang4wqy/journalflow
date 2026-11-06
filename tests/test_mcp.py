import asyncio
import sys

import pytest

from journalflow.cli import main

from .conftest import make_pdf


def test_stdio_returns_the_correct_page_from_an_imported_pdf(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = tmp_path / "local library.toml"
    options = ["--config", str(config)]
    pdf = make_pdf(tmp_path / "demo.pdf", (
        "We describe the experiment and the sampling assumptions.",
        "The route disruption experiment used an unchanged comparison group.",
        "",
    ))
    doi = "10.99999/journalflow-demo"
    assert main(options + ["init"]) == 0
    assert main(options + ["import-pdf", str(pdf), "--doi", doi, "--journal", "IJOC",
                           "--title", "Synthetic test", "--year", "2025"]) == 0
    assert main(options + ["index-text"]) == main(options + ["index-text"]) == 0

    async def check():
        params = StdioServerParameters(command=sys.executable, args=["-m", "journalflow", *options, "mcp"])
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == {"search_papers", "get_page"}
            args = dict(query="route disruption", year=2025, journal="IJOC", doi="https://doi.org/" + doi)
            result = await session.call_tool("search_papers", args)
            assert not result.isError, result.content
            source, = result.structuredContent["sources"]
            assert source["pdf_page"] == 2 and source["doi"] == doi and source["pdf_url"].endswith("#page=2")
            assert "comparison group" in source["text"]
            assert (await session.call_tool("search_papers", args | {"year": 2024})).structuredContent == {
                "sources": []}
            page_args = dict(paper_id=source["paper_id"], pdf_page=2)
            page = (await session.call_tool("get_page", page_args)).structuredContent["page"]
            assert page["text"] == "The route disruption experiment used an unchanged comparison group."
            blank = await session.call_tool("get_page", page_args | {"pdf_page": 3})
            assert blank.structuredContent["page"] is None

    asyncio.run(check())
