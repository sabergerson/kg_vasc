"""Drive KG download, transform, merge steps."""

# For debugging
import pdb

import os
from pathlib import Path
from pprint import pprint
from typing import Union

import click

try:
    from kg_chat.app import create_app
    from kg_chat.implementations import DuckDBImplementation, Neo4jImplementation
    from kg_chat.main import KnowledgeGraphChat
    from kg_chat.utils import (
        get_anthropic_models,
        get_database_impl,
        get_lbl_cborg_models,
        get_llm_config,
        get_ollama_models,
        get_openai_models,
    )
except ImportError:
    # Handle the case where kg-chat is not installed
    create_app = None
    DuckDBImplementation = None
    Neo4jImplementation = None
    KnowledgeGraphChat = None

from kg_vasc import download as kg_download
from kg_vasc.merge_utils.merge_kg import load_and_merge
from kg_vasc.query import parse_query_yaml, result_dict_to_tsv, run_query
from kg_vasc.transform import DATA_SOURCES
from kg_vasc.transform import transform as kg_transform

# Added to resolve error
OPEN_AI_MODEL = "gpt-4"


@click.group()
def main():
    """CLI."""
    pass


@main.command()
@click.option(
    "yaml_file", "-y", required=True, default="download.yaml", type=click.Path(exists=True)
)
@click.option("output_dir", "-o", required=True, default="data/raw")
# @click.option(
#     "snippet_only",
#     "-x",
#     is_flag=True,
#     default=False,
#     help="Download only the first 5 kB of each (uncompressed) source,\
#     for testing and file checks [false]",
# )
# @click.option(
#     "ignore_cache",
#     "-i",
#     is_flag=True,
#     default=False,
#     help="ignore cache and download files even if they exist [false]",
# )
def download(*args, **kwargs) -> None:
    """
    Download from list of URLs (default: download.yaml) into data directory (default: data/raw).

    :param yaml_file: Specify the YAML file containing a list of datasets to download.
    :param output_dir: A string pointing to the directory to download data to.
    :param snippet_only: Download 5 kB of each uncompressed source, for testing and file checks.
    :param ignore_cache: If specified, will ignore existing files and download again.
    :return: None
    """
    kg_download(*args, **kwargs)

    return None


@main.command()
@click.option("input_dir", "-i", default="data/raw", type=click.Path(exists=True))
@click.option("output_dir", "-o", default="data/transformed")
@click.option("sources", "-s", default=None, multiple=True, type=click.Choice(DATA_SOURCES.keys()))
def transform(*args, **kwargs) -> None:
    """
    Call project_name/transform/[source name]/ for node & edge transforms.

    :param input_dir: A string pointing to the directory to import data from.
    :param output_dir: A string pointing to the directory to output data to.
    :param sources: A list of sources to transform.
    :return: None
    """
    # call transform script for each source
    kg_transform(*args, **kwargs)

    return None


@main.command()
@click.option("yaml", "-y", default="merge.yaml", type=click.Path(exists=True))
@click.option("processes", "-p", default=1, type=int)
def merge(yaml: str, processes: int) -> None:
    """
    Use KGX to load subgraphs to create a merged graph.

    :param yaml: A string pointing to a KGX compatible config YAML.
    :param processes: Number of processes to use.
    :return: None
    """
    load_and_merge(yaml, processes)


@main.command()
@click.option("yaml", "-y", required=True, default=None, multiple=False)
@click.option("output_dir", "-o", default="data/queries/")
def query(
    yaml: str,
    output_dir: str,
    query_key: str = "query",
    endpoint_key: str = "endpoint",
    outfile_ext: str = ".tsv",
) -> None:
    """
    Perform a query of knowledge graph using a class contained in query_utils.

    :param yaml: A YAML file containing a SPARQL query (see queries/sparql/ for examples)
    :param output_dir: Directory to output results of query
    :param query_key: the key in the yaml file containing the query string
    :param endpoint_key: the key in the yaml file containing the sparql endpoint URL
    :param outfile_ext: file extension for output file [.tsv]
    :return: None.
    """
    query = parse_query_yaml(yaml)
    result_dict = run_query(query=query[query_key], endpoint=query[endpoint_key])
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    outfile = os.path.join(output_dir, os.path.splitext(os.path.basename(yaml))[0] + outfile_ext)
    result_dict_to_tsv(result_dict, outfile)


@main.command()
@click.option(
    "nodes",
    "-n",
    help="nodes KGX TSV file",
    default="data/merged/nodes.tsv",
    type=click.Path(exists=True),
)
@click.option(
    "edges",
    "-e",
    help="edges KGX TSV file",
    default="data/merged/edges.tsv",
    type=click.Path(exists=True),
)
@click.option(
    "output_dir", "-o", help="output directory", default="data/holdouts/", type=click.Path()
)
@click.option(
    "train_fraction",
    "-t",
    help="fraction of input graph to use in training graph [0.8]",
    default=0.8,
    type=float,
)
@click.option("validation", "-v", help="make validation set", is_flag=True, default=False)
def holdouts(*args, **kwargs) -> None:
    """
    Make holdouts for ML training.

    Given a graph (from formatted node and edge TSVs), output positive edges and negative
    edges for use in machine learning.

    To generate positive edges: a set of test positive edges equal in number to
    [(1 - train_fraction) * number of edges in input graph] are randomly selected from
    the edges in the input graph that is not part of a minimal spanning tree, such that
    removing the edge does not create new components. These edges are emitting as
    positive test edges. (If -v == true, the test positive edges are divided equally to
    yield test and validation positive edges.) These edges are then removed from the
    edges of the input graph, and these are emitted as the training edges.

    Negative edges are selected by randomly selecting pairs of nodes that are not
    connected by an edge in the input graph. The number of negative edges emitted is
    equal to the number of positive edges emitted above.

    Outputs these files in [output_dir]:
        pos_train_edges.tsv - positive edges for training (this is the input graph with
                      test [and validation] positive edges removed)
        pos_test_edges.tsv - positive edges for testing
        pos_valid_edges.tsv (optional) - positive edges for validation
        neg_train.tsv - a set of edges not present in input graph for training
        neg_test.tsv - a set of edges not present in input graph for testing
        neg_valid.tsv (optional) - a set of edges not present in input graph for
                      validation

    :param nodes:   nodes for input graph, in KGX TSV format [data/merged/nodes.tsv]
    :param edges:   edges for input graph, in KGX TSV format [data/merged/edges.tsv]
    :param output_dir:     directory to output edges and new graph [data/edges/]
    :param train_fraction: fraction of edges to emit as training [0.8]
    :param validation:     should we make validation edges? [False]

    """
    # make_holdouts(*args, **kwargs)
    pass


if create_app:
    # ! kg-chat must be installed for these CLI commands to work.
    ALL_AVAILABLE_PROVIDERS = ["openai", "ollama", "anthropic", "cborg"]
    ALL_AVAILABLE_MODELS = (
        get_openai_models() + get_ollama_models() + get_anthropic_models() + get_lbl_cborg_models()
    )
    ALL_AVAILABLE_DB = ["neo4j", "duckdb"]

    database_options = click.option(
        "--database",
        "-d",
        type=click.Choice(ALL_AVAILABLE_DB, case_sensitive=False),
        help="Database to use.",
        default="neo4j",
    )
    data_dir_option = click.option(
        "--data-dir",
        type=click.Path(exists=True, file_okay=False, dir_okay=True),
        help="Directory containing the data.",
        required=True,
    )
    llm_provider_option = click.option(
        "--llm-provider",
        type=click.Choice(ALL_AVAILABLE_PROVIDERS, case_sensitive=False),
        help="Language model to use.",
        required=False,
    )
    llm_option = click.option(
        "--llm",
        type=click.Choice(ALL_AVAILABLE_MODELS, case_sensitive=False),
        help="Language model to use.",
        required=False,
    )

    @main.command("import")
    @database_options
    @data_dir_option
    @llm_provider_option
    def import_kg(database: str = "neo4j", data_dir: str = None, llm_provider: str = "openai"):
        """Run the kg-chat's import command."""
        
        # For debugging
        # pdb.set_trace()

        if not data_dir:
            raise ValueError(
                "Data directory is required. This typically contains the KGX tsv files."
            )

        config = get_llm_config(llm_provider)
        print("Says neo4j")
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)
        impl.load_kg()

    @main.command()
    @data_dir_option
    @database_options
    @llm_provider_option
    @llm_option
    def test_query(
        data_dir: Union[str, Path], llm_provider: str, llm: str, database: str = "duckdb"
    ):
        """Run the kg-chat's test-query command."""
        if llm_provider is None and llm is None:
            llm = OPEN_AI_MODEL
        config = get_llm_config(llm_provider, llm)
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)

        query = (
            "MATCH (n) RETURN n LIMIT 10" if database == "neo4j" else "SELECT * FROM nodes LIMIT 10"
        )
        result = impl.execute_query(query)
        for record in result:
            print(record)

    @main.command()
    @data_dir_option
    @database_options
    @llm_provider_option
    @llm_option
    def show_schema(
        data_dir: Union[str, Path], llm_provider: str, llm: str, database: str = "duckdb"
    ):
        """Run the kg-chat's show-schema command."""
        config = get_llm_config(llm_provider, llm)
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)
        impl.show_schema()

    @main.command()
    @database_options
    @click.argument("query", type=str, required=True)
    @data_dir_option
    @llm_provider_option
    @llm_option
    def qna(
        query: str,
        data_dir: Union[str, Path],
        llm_provider: str,
        llm: str,
        database: str = "duckdb",
    ):
        """Run the kg-chat's query command."""
        config = get_llm_config(llm_provider, llm)
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)
        response = impl.get_human_response(query)
        pprint(response)

    @main.command("chat")
    @data_dir_option
    @database_options
    @llm_provider_option
    @llm_option
    def run_chat(data_dir: Union[str, Path], llm_provider: str, llm: str, database: str = "duckdb"):
        """Run the kg-chat's chat command."""
        config = get_llm_config(llm_provider, llm)
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)
        kgc = KnowledgeGraphChat(impl)
        kgc.chat()

    @main.command("app")
    @click.option("--debug", is_flag=True, help="Run the app in debug mode.")
    @data_dir_option
    @database_options
    @llm_provider_option
    @llm_option
    def run_app(
        data_dir: Union[str, Path],
        llm_provider: str,
        llm: str,
        database: str = "duckdb",
        debug: bool = False,
    ):
        """Run the kg-chat's app command."""
        config = get_llm_config(llm_provider, llm)
        impl = get_database_impl(database, data_dir=data_dir, llm_config=config)
        kgc = KnowledgeGraphChat(impl)
        app = create_app(kgc)
        # use_reloader=False to avoid running the app twice in debug mode
        app.run(debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
