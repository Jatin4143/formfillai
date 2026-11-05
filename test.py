"""
Visualize the actual compiled FieldFlow LangGraph.

Run:
    python visualize_langgraph.py

Requirements:
    pip install langgraph langchain-core ollama

Optional:
    Install Graphviz if Mermaid PNG rendering reports a missing dependency.
"""

from pathlib import Path

from IPython.display import Image, display

from demo_paragraph import FormChatService


def main():
    # This initializes the database (if needed) and builds the compiled graph.
    service = FormChatService()

    # Get the graph structure, including nested/conditional details.
    graph = service.graph.get_graph(xray=True)

    # Render Mermaid to PNG bytes.
    png_bytes = graph.draw_mermaid_png()

    # Save the diagram beside this script.
    output_path = Path(__file__).resolve().parent / "langgraph_workflow.png"
    output_path.write_bytes(png_bytes)

    print(f"LangGraph diagram saved to: {output_path}")

    # Display inline when run in a Jupyter/IPython environment.
    try:
        display(Image(data=png_bytes))
    except Exception:
        print("PNG created successfully. Open langgraph_workflow.png to view it.")


if __name__ == "__main__":
    main()
