import host_ports
async def run_evidence(evidence, llm, **kwargs):
    if host_ports.evidence_writer is None:
        raise RuntimeError('No host evidence writer is configured; no world-book update was made')
    return await host_ports.evidence_writer(evidence, llm, **kwargs)
