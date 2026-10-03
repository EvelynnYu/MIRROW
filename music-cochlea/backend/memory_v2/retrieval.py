import host_ports
async def search_records(query, limit=3, **kwargs):
    if host_ports.memory_search is None: return []
    return await host_ports.memory_search(query, limit=limit)
