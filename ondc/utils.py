from datetime import date
def build_frequency(frequency:str,repeat: int) -> str:
    freq_map = {
        "daily": "P1D",
        "weekly": "P1W",
        "monthly": "P1M",
        "quarterly": "P3M",
        "yearly": "P1Y",
    }

    if frequency not in freq_map:
        raise ValueError("Invalid frequency selected")
    
    start_date =  date.today().isoformat()  

    duration = freq_map[frequency]
    return f"R{repeat}/{start_date}/{duration}"

def get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0]
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip
