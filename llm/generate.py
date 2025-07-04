import os
from transformers import AutoTokenizer, AutoModelForCausalLM
import torch
from llm.retrieval import get_query_results, setup_vector_search_index, get_all_multi_experiment_documents, get_all_single_experiment_documents
from huggingface_hub import InferenceClient
from dotenv import load_dotenv
import pandas as pd
import io
import re
import requests
import json

load_dotenv()

# Change to a small model that's definitely available on the free tier
model_name = os.getenv("MODEL_NAME")

# Read FloodNS framework.md as static context for all prompts
def get_framework_context():
    """Read FloodNS framework.md to provide key concepts as context for LLM prompts"""
    framework_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                               "floodns", "doc", "framework.md")
    try:
        with open(framework_path, "r") as f:
            framework_content = f.read()
        return framework_content
    except Exception as e:
        return "Framework document could not be loaded. Key concepts include: Network, Node, Link, Flow, Connection, Event, Aftermath, and Simulator."

# Load the framework context once when the module is imported
FRAMEWORK_CONTEXT = get_framework_context()


def generate_with_ollama(prompt, model_name="deepseek-r1:1.5b"):
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "model": model_name,
                "prompt": prompt,
                "stream": False
            })
        )
        response.raise_for_status()
        result = response.json()
        return result.get("response", "").strip()
    except Exception as e:
        return "There was an error calling the local model through Ollama."


def generate_response(query, run_dir=None):
    """Generate a response using DeepSeek model based on retrieved context"""
    try:
        # First check if this is a bandwidth analysis question
        if is_bandwidth_query(query):
            try:
                # Import here to avoid circular imports
                from llm.bandwidth_analysis import analyze_bandwidth_for_chat
                
                # Only use bandwidth analysis if we have a run directory
                if run_dir:
                    return analyze_bandwidth_for_chat(run_dir=run_dir, query=query)
                    except Exception as e:
            # Continue with standard response generation if bandwidth analysis fails
            pass
        
        # Check if this is a request for step-by-step reasoning
        if any(phrase in query.lower() for phrase in ["step by step", "explain your thinking", "show your work", "reasoning"]):
            return generate_response_with_reasoning(query)
        
        # Standard response generation
        # First, check if we have multi-experiment data
        all_multi_docs = get_all_multi_experiment_documents()
        
        if all_multi_docs:
            # We have multi-experiment data - use comprehensive approach
            context_docs = all_multi_docs
        else:
            # Single experiment - get ALL documents for comprehensive analysis
            all_single_docs = get_all_single_experiment_documents()
            if all_single_docs:
                context_docs = all_single_docs
            else:
                return "I couldn't find any simulation data to answer your question."
        
        # Extract text, filenames, and experiment info for context
        contexts = []
        filenames = []
        experiments_found = set()
        
        for doc in context_docs:
            filename = doc.get("filename", "unknown file")
            text = doc.get("text", "")
            experiment_name = doc.get("experiment_name", "")
            experiment_params = doc.get("experiment_params", "")
            
            if text:
                if experiment_name:
                    # Multi-experiment context
                    experiments_found.add(experiment_name)
                    # For comprehensive multi-experiment analysis, use strategic excerpts
                    if len(context_docs) > 50:
                        # Many documents - use very short excerpts but include key data
                        text_length = 400
                    elif len(context_docs) > 20:
                        text_length = 600
                    else:
                        text_length = 1000
                    
                    # Extract key statistics if it's a CSV file
                    excerpt = text[:text_length]
                    if filename.endswith('.csv') and '\n' in text:
                        lines = text.split('\n')
                        if len(lines) > 10:
                            # For CSV files, include header + first few rows + last few rows
                            excerpt = '\n'.join(lines[:5] + ['...'] + lines[-3:])
                    
                    contexts.append(f"From {experiment_name} - {filename} (Parameters: {experiment_params}):\n{excerpt}...")
                else:
                    # Single experiment context
                    contexts.append(f"From {filename}:\n{text[:1000]}...")
                filenames.append(filename)
        
        # Combine contexts
        context_string = "\n\n".join(contexts)
        
        # Detect if this is a multi-experiment analysis
        is_multi_experiment = len(experiments_found) > 1
        
        # Build the RAG prompt with framework context
        if is_multi_experiment:
            # Create a comprehensive summary of all available data
            files_by_experiment = {}
            params_by_experiment = {}
            
            for doc in context_docs:
                exp_name = doc.get("experiment_name", "Unknown")
                filename = doc.get("filename", "unknown")
                params = doc.get("experiment_params", "")
                
                if exp_name not in files_by_experiment:
                    files_by_experiment[exp_name] = set()
                    params_by_experiment[exp_name] = params
                files_by_experiment[exp_name].add(filename)
            
            # Create experiment summary
            experiment_summary = []
            for exp_name, files in files_by_experiment.items():
                params = params_by_experiment.get(exp_name, "")
                experiment_summary.append(f"**{exp_name}** (Parameters: {params}): {len(files)} files - {', '.join(sorted(files))}")
            
            prompt = f"""You are an AI assistant performing COMPREHENSIVE COMPARATIVE ANALYSIS across multiple network simulation experiments.

IMPORTANT: You have access to ALL simulation data from ALL selected experiments. Use this complete dataset to provide thorough comparative analysis.

**ANALYSIS SCOPE:**
- {len(experiments_found)} different experiments: {', '.join(experiments_found)}
- {len(context_docs)} total data files analyzed
- Complete data coverage for accurate comparison

**EXPERIMENT DETAILS:**
{chr(10).join(experiment_summary)}

**ANALYSIS INSTRUCTIONS:**
- Compare data across ALL experiments
- Identify patterns, differences, and performance metrics
- For "which performed best" questions, analyze all relevant metrics and provide rankings
- Extract specific numbers and statistics from the data
- Clearly identify which experiment each data point comes from

## FloodNS Framework Concepts:
{FRAMEWORK_CONTEXT}

## COMPLETE Multi-Experiment Dataset ({len(context_docs)} files):
{context_string}

**User Question:** {query}

**Provide comprehensive comparative analysis based on ALL available simulation data:**"""
        else:
            prompt = f"""You are an AI assistant performing COMPREHENSIVE ANALYSIS of network simulation data.

IMPORTANT: You have access to ALL simulation files from this experiment. Use this complete dataset to provide thorough analysis.

**ANALYSIS SCOPE:**
- {len(context_docs)} total data files analyzed
- Complete data coverage for accurate analysis

**ANALYSIS INSTRUCTIONS:**
- Analyze ALL available data files for comprehensive insights
- Extract specific numbers, statistics and factual information from the provided data
- If the data contains CSV content, analyze the structure and count unique entries if needed
- For node counts, count unique node IDs. For bandwidth questions, look for numerical values
- Provide detailed analysis based on ALL available simulation data

## FloodNS Framework Concepts:
{FRAMEWORK_CONTEXT}

## COMPLETE Single-Experiment Dataset ({len(context_docs)} files):
{context_string}

**User Question:** {query}

**Provide comprehensive analysis based on ALL available simulation data:**"""
        
        try:
            # Check whether to use local Ollama model or HuggingFace API
            use_local = os.getenv("USE_LOCAL_MODEL", "false").lower() == "true"
            
            if use_local:
                response = generate_with_ollama(prompt)
            else:
                response = generate_with_api(prompt, context_docs, query)
                
            # Add reasoning block after the main answer
            if is_multi_experiment:
                context_summary = "\n".join([f"- {doc.get('experiment_name', 'Unknown')}/{doc.get('filename', 'unknown')}" for doc in context_docs])
                sources_info = f"""
Retrieved ALL {len(context_docs)} documents from {len(experiments_found)} experiments ({', '.join(experiments_found)}):
{context_summary}
"""
                # Return final response with only sources for multi-experiment (no generic reasoning)
                return f"{response}\n\n<sources>\n{sources_info}\n</sources>"
            else:
                context_summary = "\n".join([f"- {filename}" for filename in filenames])
                context_preview = "\n".join([doc.get("text", "")[:100] + "..." for doc in context_docs])
                
                sources_info = f"""
Retrieved ALL {len(context_docs)} documents from single experiment:
{context_summary}

Used context:
{context_preview}
"""
                # Return final response with only sources for single experiment (no generic reasoning)
                return f"{response}\n\n<sources>\n{sources_info}\n</sources>"
                
        except Exception as e:
            error_msg = str(e)
            return fallback_parser(query, context_docs)
    except Exception as e:
        return f"I had trouble searching through the simulation data. Please try again or ask an administrator to check the vector search configuration."


def is_bandwidth_query(query):
    """
    Detect if the query is asking about bandwidth analysis.
    
    This function checks if the query contains keywords related to bandwidth,
    specifically with the flow_bandwidth.csv file.
    """
    query_lower = query.lower()
    
    # Check for bandwidth keywords in combination with file references
    bandwidth_keywords = ['bandwidth', 'throughput', 'speed', 'data rate']
    file_keywords = ['flow', 'flow_bandwidth', 'flow bandwidth', 'flow_bandwidth.csv']
    
    bandwidth_match = any(kw in query_lower for kw in bandwidth_keywords)
    file_match = any(kw in query_lower for kw in file_keywords)
    
    # Check if the query is asking about averages, statistics, etc.
    stat_keywords = ['average', 'avg', 'mean', 'statistics', 'calculate', 'median', 'min', 'max']
    stat_match = any(kw in query_lower for kw in stat_keywords)
    
    # Return true if it's likely a bandwidth query
    return (bandwidth_match and (file_match or stat_match)) or (file_match and stat_match)


def generate_response_with_reasoning(query):
    """Generate a response with step-by-step reasoning"""
    try:
        from llm.think_step_by_step import think_step_by_step
        
        # Get ALL documents for comprehensive reasoning
        all_multi_docs = get_all_multi_experiment_documents()
        
        if all_multi_docs:
            # Multi-experiment data - use all documents
            context_docs = all_multi_docs
        else:
            # Single experiment - get ALL documents
            all_single_docs = get_all_single_experiment_documents()
            if all_single_docs:
                context_docs = all_single_docs
            else:
                return "I couldn't find any simulation data to reason about."
        
        # Extract text and filenames for context
        contexts = []
        for doc in context_docs:
            filename = doc.get("filename", "unknown file")
            text = doc.get("text", "")
            if text:
                contexts.append(f"From {filename}:\n{text[:1500]}...")  # Include more context
        
        # Combine contexts
        context_string = "\n\n".join(contexts)
        
        # Add framework concepts to the context
        combined_context = f"""## FloodNS Framework Concepts:
{FRAMEWORK_CONTEXT}

## Simulation Data:
{context_string}"""
        
        # Always use API for think_step_by_step
        response = think_step_by_step(query, None, combined_context, use_api=True)
        
        # Format the response to make reasoning part separate from the result
        # This will be parsed in the UI to create a dropdown
        formatted_response = f"<thinking>\n{response['reasoning']}\n</thinking>\n\n{response['result']}"
        
        return formatted_response
        
    except Exception as e:
        return f"I had trouble applying step-by-step reasoning to your question. Error: {str(e)}"


def generate_with_local_model(prompt):
    """Generate text using a local DeepSeek model if available"""
    try:
        # Load model and tokenizer
        model_name = os.getenv("MODEL_NAME")
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        
        # Try to use GPU if available, otherwise use CPU
        device_map = "auto" if torch.cuda.is_available() else {"": "cpu"}
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            trust_remote_code=True,
            device_map=device_map,
            torch_dtype=dtype
        )
        
        # Generate response
        input_ids = tokenizer(prompt, return_tensors="pt").input_ids
        
        # Move input to correct device
        if torch.cuda.is_available():
            input_ids = input_ids.to("cuda")
        
        # Generate with appropriate parameters
        with torch.no_grad():
            output = model.generate(
                input_ids,
                max_new_tokens=150,
                temperature=0.7,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id
            )
        
        # Decode and return the response
        response = tokenizer.decode(output[0], skip_special_tokens=True)
        
        # Extract only the answer part after the prompt
        answer = response[len(prompt):]
        return answer.strip()
    
    except Exception as e:
        return generate_with_api(prompt)

def generate_with_api(prompt, context_docs=None, query=None):
    """Generate text using the Hugging Face Inference API"""
    try:        
        # Initialize the client
        hf_token = os.getenv("HF_TOKEN")
        model_name = os.getenv("MODEL_NAME")
        if not hf_token:
            client = InferenceClient()
        else:
            client = InferenceClient(token=hf_token)
        
        # Generate response using the API
        response = client.text_generation(
            prompt=prompt,
            model=model_name,
            max_new_tokens=150,
            temperature=0.7,
            repetition_penalty=1.1
        )
        
        # Extract the answer part (remove the prompt)
        answer = response[len(prompt):] if len(response) > len(prompt) else response
        return answer.strip()
        
    except Exception as e:
        # Use the fallback parser if context and query are available
        if context_docs and query:
            return fallback_parser(query, context_docs)
        
        # Manual fallback - extract information from retrieved documents to provide a basic answer
        return "I'm sorry, I couldn't access the language model service. Please try again later."

def fallback_parser(query, context_docs):
    """Directly parse the simulation data when the API is unavailable"""
    query_lower = query.lower()
    
    # Check if this is multi-experiment data
    experiments_found = set()
    for doc in context_docs:
        experiment_name = doc.get("experiment_name", "")
        if experiment_name:
            experiments_found.add(experiment_name)
    
    is_multi_experiment = len(experiments_found) > 1
    
    # Extract information based on query type
    if "node" in query_lower and ("count" in query_lower or "how many" in query_lower):
        # Count unique nodes in node_info.csv
        for doc in context_docs:
            if doc.get("filename") == "node_info.csv":
                text = doc.get("text", "")
                if text:
                    try:
                        # Parse CSV content
                        df = pd.read_csv(io.StringIO(text), header=None)
                        # First column typically contains node IDs
                        node_count = df[0].nunique()
                        return f"Based on the node_info.csv file, there are {node_count} unique nodes in the simulation."
                    except Exception as e:
                        # Try a basic line count approach
                        lines = text.strip().split('\n')
                        return f"Based on the node_info.csv file, there appear to be approximately {len(lines)} nodes in the simulation."
                        
    elif "bandwidth" in query_lower and "average" in query_lower:
        # Calculate average bandwidth
        for doc in context_docs:
            if "bandwidth" in doc.get("filename", "").lower():
                text = doc.get("text", "")
                if text:
                    try:
                        # Try to parse the CSV
                        df = pd.read_csv(io.StringIO(text), header=None)
                        # Look for columns that might contain bandwidth values
                        # Typically the last column in bandwidth files
                        last_col = df.columns[-1]
                        # Convert to numeric, ignoring errors
                        bandwidth_values = pd.to_numeric(df[last_col], errors='coerce')
                        # Calculate average of non-NaN values
                        avg_bandwidth = bandwidth_values.mean()
                        return f"Based on {doc.get('filename')}, the average bandwidth is approximately {avg_bandwidth:.2f}."
                    except Exception as e:
                        # Try a simpler approach with regex
                        pass
                        numbers = re.findall(r'[\d.]+', text)
                        if numbers:
                            try:
                                values = [float(num) for num in numbers if float(num) > 0]
                                if values:
                                    avg = sum(values) / len(values)
                                    return f"Based on {doc.get('filename')}, the average bandwidth is approximately {avg:.2f}."
                            except:
                                pass
    
    # General fallback for other queries
    if is_multi_experiment:
        experiment_files = {}
        for doc in context_docs:
            exp_name = doc.get("experiment_name", "Unknown")
            filename = doc.get("filename", "unknown")
            if exp_name not in experiment_files:
                experiment_files[exp_name] = []
            experiment_files[exp_name].append(filename)
        
        summary = []
        for exp_name, files in experiment_files.items():
            summary.append(f"{exp_name}: {', '.join(files)}")
        
        return f"I found relevant information from {len(experiments_found)} experiments ({', '.join(experiments_found)}) in the following files:\n{chr(10).join(summary)}\n\nHowever, I couldn't process the comparative analysis automatically. The API service is currently unavailable."
    else:
        filenames = [doc.get("filename", "unknown") for doc in context_docs]
        return f"I found relevant information in {', '.join(filenames)}, but couldn't process it automatically. The API service is currently unavailable."
