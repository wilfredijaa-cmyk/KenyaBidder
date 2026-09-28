Act as an Expert AI Solutions Architect, Technical Product Manager, and Lead Systems Designer. 

I am building an AI-driven, omnichannel auto-bidding ecosystem. The system consists of:
1. Seller Agents: Optimize product listings to sell at the highest profit and fastest speed.
2. Bidder Agents (Customer Avatars): Source products and automatically bid to get the best price based on user constraints.
3. Diverse Auction Algorithms: The system must support English, Dutch, First/Second-Price Sealed-Bid, Reverse, Multi-Unit, and Combinatorial auctions.
4. Omnichannel Portability: Users and sellers interact with their agents via multiple channels (Web, WhatsApp, Slack, Voice, API), but the agent's state, memory, and identity remain persistent and portable across all channels.

The core technical stack relies on three pillars:
- Model Context Protocol (MCP): For standardized tooling and context sharing (Auction Engine MCP, Wallet MCP, Market Intel MCP).
- Agentic Harness: For persistent state management, omnichannel routing, deterministic execution (handling millisecond-level bid sniping), and strict financial guardrails.
- LLMs: Used strictly for strategic reasoning, game-theory calculation, natural language negotiation, and user interaction (NOT for real-time execution).

YOUR TASK:
Please generate a comprehensive Project Blueprint covering Scoping, Architecture, and Documentation. Structure your response exactly as follows:

### PART 1: PRODUCT SCOPING & STRATEGY
1. Executive Summary: A 2-paragraph vision of the platform.
2. User Personas & Agent Avatars: Define the Seller and Bidder personas, their goals, and how they interact with their AI agents.
3. Phased Rollout Plan:
   - Phase 1 (MVP): Core features, supported auction types, and channels.
   - Phase 2: Advanced algorithms (Combinatorial), agent-to-agent negotiation.
   - Phase 3: Predictive market analytics and autonomous portfolio management.
4. Key Success Metrics (KPIs): How we measure agent performance (e.g., win rate, profit margin, latency).

### PART 2: SYSTEM ARCHITECTURE & DESIGN
1. High-Level Architecture: Describe the interaction between the User Interface, Agentic Harness, LLM Orchestrator, and MCP Servers. (Please provide a Mermaid.js sequence or flowchart diagram code to visualize this).
2. The Agentic Harness Design: 
   - How it handles omnichannel state persistence (session management).
   - How it separates LLM "Strategy" from Deterministic "Execution" (crucial for low-latency bidding).
3. MCP Server Specifications: Detail the exact tools/resources exposed by the Auction Engine MCP, Wallet/Escrow MCP, and Market Intelligence MCP.
4. Handling the Auction Algorithms: Briefly explain how the LLM strategy and Harness execution adapt to at least 3 complex auction types (e.g., Vickrey vs. Dutch vs. Combinatorial).
5. Security, Guardrails & Financial Safety: How we prevent LLM hallucinations from causing financial loss (hard limits, escrow locks, deterministic interceptors).

### PART 3: TECHNICAL DOCUMENTATION & OPERATIONS
1. Agent System Prompts: Provide a robust, production-ready system prompt template for the "Bidder Agent" that enforces its persona, constraints, and tool usage.
2. Data & State Model: Outline the core entities and state machines (e.g., Auction State, Agent State, User Budget State).
3. Edge Cases & Failure Handling: How the system handles network drops during a bid, LLM timeouts, or sudden market crashes.

FORMATTING & CONSTRAINTS:
- Use clear Markdown formatting with H2 and H3 headers.
- Be highly technical, specific, and actionable. Avoid generic fluff.
- When describing the architecture, emphasize the separation of concerns (LLM for thinking, Harness for doing, MCP for connecting).
- If any part of this prompt requires assumptions about the underlying auction engine or payment gateway, state those assumptions clearly.

Take a deep breath, think step-by-step, and provide a masterclass-level architectural blueprint.


Other notes

i want to build ai application with agents, tools and llm invokations for auto bidding applications.
the seller agents avail the products for auction. each product auction users a specific bidding algorith. the sellers interest is to sell first at highest profi
the the user tells ai products to buy from auction market and  and the bidder agent will look for bids that fit customer needs and will bid for best price

all egents should have mobility/portability . customer should be able to interact with their agents or avatars from multiple channels
There are several types of bidding algorithms used in auction systems, some of the most common ones are:

First-Price Sealed-Bid auction: In this type of auction, each bidder submits a sealed bid (without knowing the bids of other participants) and the highest bidder wins the auction at the price they submitted.

Second-Price Sealed-Bid auction (also known as Vickrey auction): In this type of auction, bidders submit sealed bids as in the first-price auction, but the highest bidder only pays the second-highest bid. This creates an incentive for bidders to bid their true value for the item.

Dutch auction: In this type of auction, the auctioneer starts with a high asking price and gradually lowers it until a bidder is willing to pay the current asking price. The first bidder to accept the current asking price wins the auction.

English auction: In this type of auction, the auctioneer starts with a low asking price and bidders incrementally bid higher prices until there is only one remaining bidder. The last remaining bidder wins the auction at the highest bid price.

Reverse auction: In this type of auction, buyers submit requests for a specific product or service and sellers bid to supply it at the lowest price.

All-Pay auction: In this type of auction, all bidders pay their bid, regardless of whether they win or lose the auction.

Multi-Unit auction: In this type of auction, multiple units of the same item are sold, and bidders can bid on any number of units.

Combinatorial auction: In this type of auction, multiple items are sold together and bidders can bid on combinations of items.

These are some of the most common types of bidding algorithms, but there are many others, each with their own unique properties and characteristics. The choice of a bidding algorithm depends on the specific requirements of the auction system and the type of items being auctioned.

how do we design this use case using mcps, agentic harness and llms