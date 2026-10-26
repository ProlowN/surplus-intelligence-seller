"""Hermes plugin: sell Venice DIEM-funded inference capacity on Surplus Intelligence."""

from __future__ import annotations

from . import schemas, tools

TOOLSET = "surplus-intelligence-seller"


def register(ctx) -> None:
    """Register seller tools and the /surplus-seller status command."""
    for name, schema in schemas.SCHEMAS.items():
        handler = getattr(tools, name)
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            description=schema.get("description", ""),
            emoji="🪙",
        )
    ctx.register_command(
        "surplus-seller",
        handler=tools.surplus_seller_command,
        description="Show Venice DIEM seller status on Surplus Intelligence",
        args_hint="[status|help]",
    )
