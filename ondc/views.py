from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_datetime
import uuid, json, os, requests
from threading import Thread
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.core.exceptions import ObjectDoesNotExist
from datetime import datetime
import logging
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from .models import Transaction, Message, FullOnSearch,SelectSIP,SubmissionID,OnInitSIP,OnConfirm,OnStatus,OnUpdate,PaymentSubmisssion,OnCancel
from .cryptic_utils import create_authorisation_header  
from .utils import build_frequency, get_client_ip


class ONDCSearchView(APIView):
    def post(self, request, *args, **kwargs):

        transaction_id = request.data.get('transaction_id')
        message_id = request.data.get('message_id')

        if not transaction_id or not message_id:
            transaction_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
        
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"


        # Prepare payload
        payload = {
            "context": {
                "location": {
                    "country": {"code": "IND"},
                    "city": {"code": "*"}
                },
                "domain": "ONDC:FIS14",
                "timestamp": timestamp,
                "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                "transaction_id": transaction_id,
                "message_id": message_id,
                "version": "2.0.0",
                "ttl": "PT10M",
                "action": "search"
            },
            "message": {
                "intent": {
                    "category": {
                        "descriptor": {
                            "code": "MUTUAL_FUNDS"
                        }
                    },
                    "fulfillment": {
                        "agent": {
                            "organization": {
                                "creds": [
                                    {"id": os.getenv('ARN'), "type": "ARN"}
                                ]
                            }
                        }
                    },
                    "tags": [
                        {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                    "descriptor": {
                                        "name": "Static Terms (Transaction Level)",
                                        "code": "STATIC_TERMS"
                                    },
                                    "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                    "descriptor": {
                                        "name": "Offline Contract",
                                        "code": "OFFLINE_CONTRACT"
                                    },
                                    "value": "true"
                                }
                            ]
                        }
                    ]
                }
            }
        }

        # Store transaction and message
        transaction, _ = Transaction.objects.get_or_create(transaction_id=transaction_id)
        Message.objects.create(
            transaction=transaction,
            message_id=message_id,
            action="search",
            timestamp=parse_datetime(timestamp),
            payload=payload
        )

        # Send to gateway
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post("https://preprod.gateway.ondc.org/search", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)



logger = logging.getLogger(__name__)

class OnSearchView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            data = request.data  
            logger.info("Received on_search callback:\n%s", json.dumps(data, indent=2))
            print("Received on_search callback:\n", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")

            # Validate required fields
            if not all([message_id, transaction_id, timestamp_str]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {"status": "NACK"},
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Parse timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {"status": "NACK"},
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Get related transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {"status": "NACK"},
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)

            # Save to database
            FullOnSearch.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_search data: %s", str(e), exc_info=True)
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {"status": "NACK"},
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Success response
        return Response({
            "context": context,
            "message": {
                "ack": {"status": "ACK"}
            }
        }, status=status.HTTP_200_OK)



class OnSearchDataView(APIView):
    def post(self, request, *args, **kwargs):
        transaction_id = request.data.get("transaction_id")
        
        if not transaction_id:
            return Response({"error": "Missing transaction_id"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            transaction = Transaction.objects.get(transaction_id=transaction_id)
            search_entries = FullOnSearch.objects.filter(transaction=transaction)

            response_data = []
            for entry in search_entries:
                response_data.append({
                    "message_id": entry.message_id,
                    "timestamp": entry.timestamp,
                    "payload": entry.payload
                })

            return Response(response_data, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"error": "Transaction not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error("Failed to fetch FullOnSearch data: %s", str(e))
            return Response({"error": "Server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
# SIP Creation Without KYC

class SIPCreationView(APIView):

    def post(self, request, *args, **kwargs):
        transaction_id = request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        preferred_type='SIP'
        amount= request.data.get('amount', '3000')  
        pan=request.data.get('pan', 'ABCDE1234F') 
        frequency=request.data.get('frequency', 'monthly') 
        repeat=request.data.get('repeat',6) 

        if not all([transaction_id, bpp_id, bpp_uri]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            FullOnSearch,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
        print(obj.payload)

        # Get the first provider and item
        provider = obj.payload["message"]["catalog"]["providers"][0]
        catalog = obj.payload["message"]["catalog"]
        matching_fulfillment = next((f for f in provider["fulfillments"] if f.get("type") == preferred_type),
        None
    )

        if not matching_fulfillment:
            return Response({"error": f"No fulfillment with type '{preferred_type}' found."},
                            status=status.HTTP_404_NOT_FOUND)


        payload = {
  "context": {
    "location": {
      "country": {
        "code": "IND"
      },
      "city": {
        "code": "*"
      }
    },
    "domain": "ONDC:FIS14",
   "timestamp": timestamp,
    "bap_id": "investment.preprod.vyable.in",
    "bap_uri": "https://investment.preprod.vyable.in/ondc",
    "transaction_id": transaction_id,
    "message_id": message_id,
    "version": "2.0.0",
    "ttl": "PT10M",
    "bpp_id": bpp_id,
    "bpp_uri": bpp_uri,
    "action": "select"
  },
  "message": {
    "order": {
      "provider": {
        "id": provider['id']
      },
      "items": [
        {
          "id": provider['items'][0]['id'],
          "quantity": {
            "selected": {
              "measure": {
                "value": amount,
                "unit": "INR"
              }
            }
          }
        }
      ],
      "fulfillments": [
        {
          "id": matching_fulfillment['id'],
          "type": matching_fulfillment['type'],
          "customer": {
            "person": {
              "id": "pan:"+pan
            }
          },
          "agent": {
            "person": {
              "id": os.getenv('EUIN')
            },
            "organization": {
              "creds": [
                {
                  "id": os.getenv('ARN'),
                  "type": "ARN"
                },
                {
                  "id": os.getenv('ARN'),
                  "type": "SUB_BROKER_ARN"
                }
              ]
            }
          },
          "stops": [
            {
              "time": {
                "schedule": {
                  "frequency":build_frequency(frequency, repeat)
                }
              }
            }
          ]
        }
      ],
      "tags": [
        {
          "display": False,
          "descriptor": {
            "name": "BAP Terms of Engagement",
            "code": "BAP_TERMS"
          },
          "list": [
            {
              "descriptor": {
                "name": "Static Terms (Transaction Level)",
                "code": "STATIC_TERMS"
              },
              "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
            },
            {
              "descriptor": {
                "name": "Offline Contract",
                "code": "OFFLINE_CONTRACT"
              },
              "value": "true"
            }
          ]
        }
      ]
    }
  }
}
        
        transaction = Transaction.objects.get(transaction_id=transaction_id)
        Message.objects.create(
            transaction=transaction,
            message_id=message_id,
            action="select",
            timestamp=parse_datetime(timestamp),
            payload=payload
        )

        # Send to gateway
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers)
        

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)                

logger = logging.getLogger(__name__)

class OnSelectSIPView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            data = request.data
            logger.info("Received on_select payload: %s", data)
            print("Received on_select payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

            # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            if action != "on_select":
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "ACTION-MISMATCH",
                            "message": "Invalid action. Expected 'on_select'"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)

            # Save data
            SelectSIP.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_select: %s", str(e), exc_info=True)
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {
                        "status": "NACK"
                    },
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Success response
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        }, status=status.HTTP_200_OK)



# form Submission

class FormSubmisssion(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')

        if not all([transaction_id, bpp_id, bpp_uri]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(SelectSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id)
        message_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            xinput=obj.payload["message"]["order"]["xinput"]
            url = obj.payload["message"]["order"]["xinput"]["form"]["url"]
        except (KeyError, TypeError):
            return Response(
                {"error": "Form URL not found in payload"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user_kyc_data = {
            "pan": "ABCDE1234F",
            "dob": "1990-01-01",
            "email": "user@example.com",
            "name": "Ravi Kumar",
            "gender":"Male",
            "marital_status":"Married",
            "occupation":"Salaried",
            "source_of_wealth":"Business",
            "income_range":"1L to 5L",
            "cob":"India",
            "pob":"Kochi",
            "political_exposure":"no_exposure",
            "india_tax_residency_status":"resident",
            "mode_of_holding":"single",
            "ca_line":"hfjfk jifl jffj",

        }
        try:
            res = requests.post(url, json=user_kyc_data)
            if res.status_code == 200:
                resp_json = res.json()
                submission_id=resp_json['submission_id']
                if not submission_id:
                    return Response({"error":"submission id missing"},status=status.HTTP_400_BAD_REQUEST)
                
                SubmissionID.objects.create(
                    transaction=obj.transaction,
                    submission_id=submission_id,
                    message_id=message_id,
                    timestamp=timestamp
                )
                payload={
                            "context": {
                                "location": {
                                "country": {
                                    "code": "IND"
                                },
                                "city": {
                                    "code": "*"
                                }
                                },
                                "domain": "ONDC:FIS14",
                                "timestamp": timestamp,
                                "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                                "transaction_id": transaction_id,
                                "message_id": message_id,
                                "version": "2.0.0",
                                "ttl": "PT10M",
                                "bpp_id": bpp_id,
                                "bpp_uri":bpp_uri,
                                "action": "select"
                            },
                            "message": {
                                "order": {
                                "provider": {
                                    "id": provider['id']
                                },
                                "items": [
                                    {
                                    "id": item[0]['id'],
                                    "quantity": {
                                        "selected": {
                                        "measure": {
                                            "value": item[0]['quantity']['selected']['measure']['value'],
                                            "unit": item[0]['quantity']['selected']['measure']['unit']
                                        }
                                        }
                                    },
                                    "fulfillment_ids": [
                                        item[0]['fulfillment_ids'][0]
                                    ]
                                    }
                                ],
                                "fulfillments": [
                                    {
                                    "id": fulfillments[0]['id'],
                                    "type": fulfillments[0]['type'],
                                    "customer": {
                                        "person": {
                                        "id": fulfillments[0]['customer']['person']['id']
                                        }
                                    },
                                    "agent": {
                                        "person": {
                                        "id": fulfillments[0]['agent']['person']['id']
                                        },
                                        "organization": {
                                        "creds": [
                                            {
                                            "id":fulfillments[0]['agent']['organization']['creds'][0]['id'],
                                            "type": fulfillments[0]['agent']['organization']['creds'][0]['type']
                                            },
                                            {
                                            "id": fulfillments[0]['agent']['organization']['creds'][1]['id'],
                                            "type": fulfillments[0]['agent']['organization']['creds'][1]['type']
                                            }
                                        ]
                                        }
                                    },
                                    "stops": [
                                        {
                                        "time": {
                                            "schedule": {
                                            "frequency": fulfillments[0]['stops'][0]['time']['schedule']['frequency']
                                            }
                                        }
                                        }
                                    ]
                                    }
                                ],
                                "xinput": {
                                    "form": {
                                    "id": xinput['form']['id']
                                    },
                                    "form_response": {
                                    "submission_id": submission_id
                                    }
                                },
                                "tags": [
                                    {
                                    "display": False,
                                    "descriptor": {
                                        "name": obj.payload["message"]["order"]['tags'][0]['descriptor']['name'],
                                        "code": obj.payload["message"]["order"]['tags'][0]['descriptor']['code']
                                    },
                                    "list": [
                                        {
                                        "descriptor": {
                                            "name": obj.payload["message"]["order"]['tags'][0]['list'][0]['descriptor']['name'],
                                            "code": obj.payload["message"]["order"]['tags'][0]['list'][0]['descriptor']['code']
                                        },
                                        "value": obj.payload["message"]["order"]['tags'][0]['list'][0]['value']
                                        },
                                        {
                                        "descriptor": {
                                            "name": obj.payload["message"]["order"]['tags'][0]['list'][1]['descriptor']['name'],
                                            "code": obj.payload["message"]["order"]['tags'][0]['list'][1]['descriptor']['code']
                                        },
                                        "value":obj.payload["message"]["order"]['tags'][0]['list'][1]['value']
                                        }
                                    ]
                                    }
                                ]
                                }
                            }
                            }  
                 # Send to gateway
                request_body_str = json.dumps(payload, separators=(',', ':'))
                auth_header = create_authorisation_header(request_body=request_body_str)

                headers = {
                    "Content-Type": "application/json",
                    "Authorization": auth_header,
                    "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                    "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
                }

                response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
                return Response({
                        "status_code": response.status_code,
                        "response": response.json() if response.content else {}
                    }, status=status.HTTP_200_OK)      
            else:
                return Response(
                    {"error": f"Form upload failed with status {res.status_code}"},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except requests.exceptions.RequestException as e:
            return Response(
                {"error": f"Form upload failed: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": f"Unexpected error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )   
                                
                       


class INIT(APIView):
    
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')
        phone= request.data.get('phone', '123456789')
        ifsc=request.data.get('ifsc', 'HDFC0000089')
        account_number=request.data.get('account_number', '004701563111')
        name=request.data.get('name', 'Harish Gupta')
        acs_type=request.data.get('acs_type', 'SAVINGS')
        payment_mode=request.data.get('payment_mode', 'UPI')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(SelectSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_init = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except (KeyError, TypeError) as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id_init,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "init"

                },
                "message": {
                    "order": {
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": item[0]['quantity']['selected']['measure']['value'],
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            item[0]['fulfillment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type": fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": fulfillments[0]['customer']['person']['id'],
                            "creds": [
                               {
                                    "id": get_client_ip(request),
                                    "type": "IP_ADDRESS"
                                }
                            ]
                        },
                        "contact": {
                            "phone": phone
                        }
                    },
                            
                        "agent": {
                            "person": {
                            "id": fulfillments[0]['agent']['person']['id']
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": fulfillments[0]['agent']['organization']['creds'][0]['id'],
                                "type": fulfillments[0]['agent']['organization']['creds'][0]['type']
                                },
                                {
                                "id": fulfillments[0]['agent']['organization']['creds'][1]['id'],
                                "type": fulfillments[0]['agent']['organization']['creds'][1]['type']
                                }
                            ]
                            }
                        },
                        "stops": [
                            {
                            "time": {
                                "schedule": {
                                "frequency": fulfillments[0]["tags"][0]["list"][0]["value"]
                                }
                            }
                            }
                        ]
                        }
                    ],
                    "payments": [
                        {
                        "collected_by": payments[0]['collected_by'],
                        "params": {
                            "amount": item[0]['quantity']['selected']['measure']['value'],
                            "currency": "INR",
                            "source_bank_code": ifsc,
                            "source_bank_account_number": account_number,
                            "source_bank_account_name": payments[1]['tags'][0]['list'][4]['value']
                        },
                        "type": payments[0]['type'],
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Source bank account",
                                "code": "SOURCE_BANK_ACCOUNT"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Account Type",
                                    "code": "ACCOUNT_TYPE"
                                },
                                "value": acs_type
                                }
                            ]
                            },
                            {
                            "descriptor": {
                                "name": "Payment Method",
                                "code": "PAYMENT_METHOD"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "code": "MODE"
                                },
                                "value": payments[0]['tags'][0]['list'][0]['value']
                                },
                                {
                                "descriptor": {
                                    "code": "AUTH"
                                },
                                "value": "NETBANKING"
                                },
                                {
                                "descriptor": {
                                    "code": "MANDATE_LIMIT"
                                },
                                "value": fulfillments[0]['tags'][0]['list'][3]['value']
                                }
                            ]
                            }
                        ]
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": obj.payload['message']['order']['tags'][0]['list'][0]['value']
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": obj.payload['message']['order']['tags'][0]['list'][1]['value']
                            }
                        ]
                        }
                    ]
                    }
                }
                }
         # Send to gateway
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)                


class ONINIT(APIView):
    def post(self, request, *args, **kwargs):
        try:
            data = request.data
            logger.info("Received on_init payload: %s", data)
            print("Received on_init payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

            # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            if action != "on_init":
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "ACTION-MISMATCH",
                            "message": "Invalid action. Expected 'on_init'"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)

            # Save to database
            OnInitSIP.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_init data: %s", str(e))
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {
                        "status": "NACK"
                    },
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Success response
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        }, status=status.HTTP_200_OK)
    


class ConfirmSIP(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnInitSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_conform = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            id=obj.payload['message']['order']['id']
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except (KeyError, TypeError) as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            payment_method = payments[0]['tags'][0]['list'][0]['value']
        except (IndexError, KeyError):
            return Response(
                {"error": "Missing payment method in payment tags"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Determine payment type based on payment method
        if payment_method == "MANDATE_REGISTRATION":
            payment_type = "PRE_FULFILLMENT"
        elif payment_method == "UPI_ON_DELIVERY":
            payment_type = "ON_FULFILLMENT"
        else:
            payment_type = "POST_FULFILLMENT"

        payload={
  "context": {
    "location": {
      "country": {
        "code": "IND"
      },
      "city": {
        "code": "*"
      }
    },
    "domain": "ONDC:FIS14",
    "timestamp": timestamp,
"bap_id": "investment.preprod.vyable.in",
"bap_uri": "https://investment.preprod.vyable.in/ondc",    
"transaction_id": transaction_id,
    "message_id": message_id_conform,
    "version": "2.0.0",
    "ttl": "PT10M",
    "bpp_id": bpp_id,
    "bpp_uri":bpp_uri,
    "action": "confirm"
  },
  "message": {
    "order": {
      "id": id,
      "provider": {
        "id": provider['id']
      },
      "items": [
        {
          "id": item[0]['id'],
          "quantity": {
            "selected": {
              "measure": {
                "value": item[0]['quantity']['selected']['measure']['value'],
                "unit": "INR"
              }
            }
          },
          "fulfillment_ids": [
            item[0]['fulfillment_ids'][0]
          ],
          "payment_ids": [
            item[0]['payment_ids'][0]
          ]
        }
      ],
      "fulfillments": [
        {
          "id": fulfillments[0]['id'],
          "type": fulfillments[0]['type'],
          "customer": {
            "person": {
              "id": fulfillments[0]['customer']['person']['id'],
              "creds": [
                {
                  "id": fulfillments[0]['customer']['person']['creds'][0]['id'],
                  "type": "IP_ADDRESS"
                }
              ]
            },
            "contact": {
              "phone": fulfillments[0]['customer']['contact']['phone']
            }
          },
          "agent": {
            "person": {
              "id": fulfillments[0]['agent']['person']['id']
            },
            "organization": {
              "creds": [
                {
                  "id": fulfillments[0]['agent']['organization']['creds'][0]['id'],
                  "type": "ARN"
                },
                {
                  "id": fulfillments[0]['agent']['organization']['creds'][1]['id'],
                  "type": "SUB_BROKER_ARN"
                }
              ]
            }
          },
          "stops": [
            {
              "time": {
                "schedule": {
                  "frequency": fulfillments[0]["tags"][0]["list"][0]["value"]
                }
              }
            }
          ]
        }
      ],
      "payments": [
        {
          "id":payments[0]['id'],
          "collected_by": payments[0]['collected_by'],
          "status": payments[0]['status'],
          "params": {
            "amount": payments[0]['params']['amount'],
            "currency": "INR",
            "source_bank_code": payments[0]['params']['source_bank_code'],
            "source_bank_account_number": payments[0]['params']['source_bank_account_number'],
            "source_bank_account_name": payments[0]['params']['source_bank_account_name'],
            "transaction_id": payments[0]['id']
          },
          "type": payment_type,
          "tags": [
            {
              "descriptor": {
                "name": payments[0]['tags'][0]['descriptor']['name'],
                "code": payments[0]['tags'][0]['descriptor']['code']
              },
              "list": [
                {
                  "descriptor": {
                    "code": payments[0]['tags'][0]['list'][0]['descriptor']['code']
                  },
                  "value": payments[0]['tags'][0]['list'][0]['value']
                }
              ]
            }
          ]
        }
      ],
      "tags": [
        {
          "display": False,
          "descriptor": {
            "name": "BAP Terms of Engagement",
            "code": "BAP_TERMS"
          },
          "list": [
            {
              "descriptor": {
                "name": "Static Terms (Transaction Level)",
                "code": "STATIC_TERMS"
              },
              "value": obj.payload["message"]["order"]['tags'][0]['list'][0]['value']
            },
            {
              "descriptor": {
                "name": "Offline Contract",
                "code": "OFFLINE_CONTRACT"
              },
              "value": obj.payload["message"]["order"]['tags'][0]['list'][1]['value']
            }
          ]
        },
        {
          "display": False,
          "descriptor": {
            "name": "BPP Terms of Engagement",
            "code": "BPP_TERMS"
          },
          "list": [
            {
              "descriptor": {
                "name": "Static Terms (Transaction Level)",
                "code": "STATIC_TERMS"
              },
              "value": obj.payload["message"]["order"]['tags'][0]['list'][0]['value']
            },
            {
              "descriptor": {
                "name": "Offline Contract",
                "code": "OFFLINE_CONTRACT"
              },
              "value": "true"
            }
          ]
        }
      ]
    }
  }
}
        # Send to gateway

        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)                


class OnConfirmSIP(APIView):
    def post(self,request,*args,**kwargs):
        try:
            data = request.data
            logger.info("Received on_confirm payload: %s", data)
            print("Received on_confirm payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

             # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            if action != "on_confirm":
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "ACTION-MISMATCH",
                            "message": "Invalid action. Expected 'on_confirm'"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)
             # Save to database
            OnConfirm.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_search data: %s", str(e))
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {
                        "status": "NACK"
                    },
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        }, status=status.HTTP_200_OK)
    
class OnStatusView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            data = request.data
            logger.info("Received on_status payload: %s", data)
            print("Received on_status payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

            # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            if action != "on_status":
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "ACTION-MISMATCH",
                            "message": "Invalid action. Expected 'on_status'"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)

            # Save to database
            OnStatus.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_status data: %s", str(e))
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {
                        "status": "NACK"
                    },
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Return success ACK
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        }, status=status.HTTP_200_OK)
   


class OnUpdateView(APIView):

    def post(self, request, *args, **kwargs):
        try:
            data = request.data
            logger.info("Received on_update payload: %s", data)
            print("Received on_update payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

            # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "CONTEXT-ERROR",
                            "message": "Missing required fields in context"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            if action != "on_update":
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "ACTION-MISMATCH",
                            "message": "Invalid action. Expected 'on_update'"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TIMESTAMP-ERROR",
                            "message": "Invalid timestamp format"
                        }
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response({
                    "context": context,
                    "message": {
                        "ack": {
                            "status": "NACK"
                        },
                        "error": {
                            "type": "TRANSACTION-NOT-FOUND",
                            "message": "Transaction not found"
                        }
                    }
                }, status=status.HTTP_404_NOT_FOUND)

            # Save to database
            OnUpdate.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_update data: %s", str(e))
            return Response({
                "context": context if 'context' in locals() else {},
                "message": {
                    "ack": {
                        "status": "NACK"
                    },
                    "error": {
                        "type": "SERVER-ERROR",
                        "message": str(e)
                    }
                }
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Success response
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        }, status=status.HTTP_200_OK)


                
# SIP Creation with Kyc

class DigiLockerFormSubmission(APIView):
         
        def post(self,request,*args,**kwargs):
            transaction_id=request.data.get('transaction_id')
            bpp_id = request.data.get('bpp_id')
            bpp_uri = request.data.get('bpp_uri')
            message_id=request.data.get('message_id')

            if not all([transaction_id,bpp_id,bpp_uri,message_id]):
                return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
            
            obj=get_object_or_404(OnStatus,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
            message_id_select = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

           
            try:
                provider=obj.payload['message']['order']['provider']
                item=obj.payload['message']['order']['items']
                xinput=obj.payload['message']['order']['xinput']
                fulfillments=obj.payload['message']['order']['fulfillments']
            except (KeyError, TypeError) as e:
                return Response(
                    {"error": f"Missing key in payload: {e}"},
                    status=status.HTTP_400_BAD_REQUEST
            )

            payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                       "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id,
                        "message_id": message_id_select,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri":bpp_uri,
                        "action": "select"
                    },
                    "message": {
                        "order": {
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type": fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            },
                            "stops": [
                                {
                                "time": {
                                    "schedule": {
                                    "frequency": fulfillments[0]['stops'][0]['time']['schedule']['frequency']
                                    }
                                }
                                }
                            ]
                            }
                        ],
                        "xinput": {
                            "form": {
                            "id": xinput['form']['id']
                            },
                            "form_response": {
                            "submission_id": xinput['form_response']['submission_id']
                            }
                        },
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
            

            # Send to gateway

            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)

            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }

            response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
            return Response({
                    "status_code": response.status_code,
                    "response": response.json() if response.content else {}
                }, status=status.HTTP_200_OK)                

class EsignFormSubmission(APIView):
    
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnStatus,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_select = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
                provider=obj.payload['message']['order']['provider']
                item=obj.payload['message']['order']['items']
                xinput=obj.payload['message']['order']['xinput']
                fulfillments=obj.payload['message']['order']['fulfillments']
        except (KeyError, TypeError) as e:
            return Response(
                    {"error": f"Missing key in payload: {e}"},
                    status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                   "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id_select,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "select"
                },
                "message": {
                    "order": {
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            item[0]['fulfillment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type": fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        },
                        "stops": [
                            {
                            "time": {
                                "schedule": {
                                "frequency": fulfillments[0]['stops'][0]['time']['schedule']['frequency']
                                }
                            }
                            }
                        ]
                        }
                    ],
                    "xinput": {
                        "form": {
                        "id": xinput['form']['id']
                        },
                        "form_response": {
                        "submission_id": xinput['form_response']['submission_id']
                        }
                    },
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
        # Send to gateway

        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   


# SIP Creation (Existing Folio - Investor selects/enters a folio)

class SIPExixstingInit(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            SelectSIP,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_init = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                       "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id,
                        "message_id":message_id_init,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri": bpp_uri,
                        "action": "init"
                    },
                    "message": {
                        "order": {
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                obj.payload['message']['order']['quote']['breakup'][0]['item']['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type":fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n",
                                "creds": [
                                    {
                                    "id": "78953432/32",
                                    "type": "FOLIO"
                                    },
                                    {
                                    "id": "115.245.207.90",
                                    "type": "IP_ADDRESS"
                                    }
                                ]
                                },
                                "contact": {
                                "phone": "9916599123"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            },
                            "stops": [
                                {
                                "time": {
                                    "schedule": {
                                    "frequency": fulfillments[0]['stops'][0]['time']['schedule']['frequency']
                                    }
                                }
                                }
                            ]
                            }
                        ],
                        "payments": [
                            {
                            "collected_by": payments[0]['collected_by'],
                            "params": {
                                "amount": "3000",
                                "currency": "INR",
                                "source_bank_code": "icic0000047",
                                "source_bank_account_number": "004701563111",
                                "source_bank_account_name": "harish gupta"
                            },
                            "type": payments[0]['type'],
                            "tags": [
                                {
                                "descriptor": {
                                    "name": "Payment Method",
                                    "code": "PAYMENT_METHOD"
                                },
                                "list": [
                                    {
                                    "descriptor": {
                                        "code": "MODE"
                                    },
                                    "value": "MANDATE_REGISTRATION"
                                    }
                                ]
                                }
                            ]
                            }
                        ],
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
        
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   

class SIPExistingConfirm(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnInitSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_confirm = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                   "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id_confirm,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "confirm"
                },
                "message": {
                    "order": {
                    "id": obj.payload['message']['order']['id'],
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            item[0]['fulfillment_ids'][0]
                        ],
                        "payment_ids": [
                        item[0]['payment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type":fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n",
                            "creds": [
                                {
                                "id": "78953432/32",
                                "type": "FOLIO"
                                },
                                {
                                "id": "115.245.207.90",
                                "type": "IP_ADDRESS"
                                }
                            ]
                            },
                            "contact": {
                            "phone": "9916599123"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        },
                        "stops": [
                            {
                            "time": {
                                "schedule": {
                                "frequency": fulfillments[0]['stops'][0]['time']['schedule']['frequency']
                                }
                            }
                            }
                        ]
                        }
                    ],
                    "payments": [
                        {
                        "id": payments[0]['id'],
                        "collected_by": payments[0]['collected_by'],
                        "status":payments[0]['status'],
                        "params": {
                            "amount": "3000",
                            "currency": "INR",
                            "source_bank_code": "icic0000047",
                            "source_bank_account_number": "004701563111",
                            "source_bank_account_name": "harish gupta",
                            "transaction_id": "243423324"
                        },
                        "type": payments[0]['type'],
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Payment Method",
                                "code": "PAYMENT_METHOD"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "code": "MODE"
                                },
                                "value": "MANDATE_REGISTRATION"
                                }
                            ]
                            }
                        ]
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        },
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BPP Terms of Engagement",
                            "code": "BPP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://sellerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   





# Sip Cancel By tHe Investor
class SIPCancel(APIView):
     def post(self,request,*ags,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        # message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        # obj=get_object_or_404(OnConfirm,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        order_id = str(uuid.uuid4())
        message_id_cancel=str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        # try:
        #     provider=obj.payload['message']['order']['provider']
        #     item=obj.payload['message']['order']['items']
        #     fulfillments=obj.payload['message']['order']['fulfillments']
        #     payments=obj.payload['message']['order']['payments']
        # except KeyError as e:
        #     return Response(
        #         {"error": f"Missing key in payload: {e}"},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )
        # except TypeError:
        #     return Response(
        #         {"error": "Invalid payload structure (possibly None or wrong type)"},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )
        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                   "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id_cancel,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "cancel"
                },
                "message": {
                    "order_id": order_id,
                    "cancellation_reason_id": "07",
                    "tags": [
                    {
                        "display": True,
                        "descriptor": {
                        "name": "Consumer Info",
                        "code": "CONSUMER_INFO"
                        },
                        "list": [
                        {
                            "descriptor": {
                            "name": "IP Address",
                            "code": "IP_ADDRESS"
                            },
                            "value": "115.245.207.90"
                        }
                        ]
                    }
                    ]
                }
                }
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/cancel", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   




class OnCancelView(APIView):
    def post(self,request,*args,**kwargs):
        try:
            data = request.data
            logger.info("Received on_confirm payload: %s", data)
            print("Received on_cancel payload:", json.dumps(data, indent=2))

            context = data.get("context", {})
            message_id = context.get("message_id")
            transaction_id = context.get("transaction_id")
            timestamp_str = context.get("timestamp")
            action = context.get("action")

             # Validate context fields
            if not all([message_id, transaction_id, timestamp_str, action]):
                return Response(
                    {"error": "Missing required fields in context"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if action != "on_cancel":
                return Response(
                    {"error": "Invalid action. Expected 'on_cancel'"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate timestamp
            timestamp = parse_datetime(timestamp_str)
            if not timestamp:
                return Response(
                    {"error": "Invalid timestamp format"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate transaction
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
            except Transaction.DoesNotExist:
                logger.warning("Transaction not found: %s", transaction_id)
                return Response(
                    {"error": "Transaction not found"},
                    status=status.HTTP_404_NOT_FOUND
                )
            
             # Save to database
            OnCancel.objects.create(
                transaction=transaction,
                message_id=message_id,
                payload=data,
                timestamp=timestamp
            )

        except Exception as e:
            logger.error("Failed to process on_search data: %s", str(e))
            return Response({"error": "Server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "on_search received"}, status=status.HTTP_200_OK)


# Lumpsum - New Folio

class Lumpsum(APIView):

    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')

        if not all([transaction_id,bpp_id,bpp_uri]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            FullOnSearch,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )

        message_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
        print(obj.payload)

        # Get the first provider and item
        provider = obj.payload["message"]["catalog"]["providers"]

        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                   "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "select"
                },
                "message": {
                    "order": {
                    "provider": {
                        "id": provider[0]['id']
                    },
                    "items": [
                        {
                        "id": provider[0]['items'][0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                        provider[0]['fulfillments'][0]['id']
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": provider[0]['fulfillments'][0]['id'],
                        "type":provider[0]['fulfillments'][0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        }
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
        
        transaction = Transaction.objects.get(transaction_id=transaction_id)
        Message.objects.create(
            transaction=transaction,
            message_id=message_id,
            action="select",
            timestamp=parse_datetime(timestamp),
            payload=payload
        )

        # Send to gateway
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)                


class LumpFormSub(APIView):

    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')

        if not all([transaction_id, bpp_id, bpp_uri]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(SelectSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id)
        message_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            xinput=obj.payload["message"]["order"]["xinput"]
            url = obj.payload["message"]["order"]["xinput"]["form"]["url"]

        except (KeyError, TypeError):
            return Response(
                {"error": "Form URL not found in payload"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        user_kyc_data = {
            "pan": "ABCDE1234F",
            "dob": "1990-01-01",
            "email": "user@example.com",
            "name": "Ravi Kumar",
            "gender":"Male",
            "marital_status":"Married",
            "occupation":"Salaried",
            "source_of_wealth":"Business",
            "income_range":"1L to 5L",
            "cob":"India",
            "pob":"Kochi",
            "political_exposure":"no_exposure",
            "india_tax_residency_status":"resident",
            "mode_of_holding":"single",
            "ca_line":"hfjfk jifl jffj",

        }
        try:
                res = requests.post(url, json=user_kyc_data)
                if res.status_code == 200:
                    resp_json = res.json()
                    submission_id=resp_json['submission_id']
                    if not submission_id:
                        return Response({"error":"submission id missing"},status=status.HTTP_400_BAD_REQUEST)
                    
                    SubmissionID.objects.create(
                        transaction=obj.transaction,
                        submission_id=submission_id,
                        message_id=message_id,
                        timestamp=timestamp
                    )

                    payload={
                                        "context": {
                                            "location": {
                                            "country": {
                                                "code": "IND"
                                            },
                                            "city": {
                                                "code": "*"
                                            }
                                            },
                                            "domain": "ONDC:FIS14",
                                            "timestamp": timestamp,
                                           "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                                            "transaction_id": transaction_id,
                                            "message_id": message_id,
                                            "version": "2.0.0",
                                            "ttl": "PT10M",
                                            "bpp_id": bpp_id,
                                            "bpp_uri": bpp_uri,
                                            "action": "select"
                                        },
                                        "message": {
                                            "order": {
                                            "provider": {
                                                "id": provider['id']
                                            },
                                            "items": [
                                                {
                                                "id": item[0]['id'],
                                                "quantity": {
                                                    "selected": {
                                                    "measure": {
                                                        "value": "3000",
                                                        "unit": "INR"
                                                    }
                                                    }
                                                },
                                                "fulfillment_ids": [
                                                    item[0]['fulfillment_ids'][0]
                                                ]
                                                }
                                            ],
                                            "fulfillments": [
                                                {
                                                "id": fulfillments[0]['id'],
                                                "type": fulfillments[0]['type'],
                                                "customer": {
                                                    "person": {
                                                    "id": "pan:arrpp7771n"
                                                    }
                                                },
                                                "agent": {
                                                    "person": {
                                                    "id": "euin:E52432"
                                                    },
                                                    "organization": {
                                                    "creds": [
                                                        {
                                                        "id": "ARN-124567",
                                                        "type": "ARN"
                                                        },
                                                        {
                                                        "id": "ARN-123456",
                                                        "type": "SUB_BROKER_ARN"
                                                        }
                                                    ]
                                                    }
                                                }
                                                }
                                            ],
                                            "xinput": {
                                                "form": {
                                                "id":  xinput['form']['id']
                                                },
                                                "form_response": {
                                                "submission_id": submission_id
                                                }
                                            },
                                            "tags": [
                                                {
                                                "display": False,
                                                "descriptor": {
                                                    "name": "BAP Terms of Engagement",
                                                    "code": "BAP_TERMS"
                                                },
                                                "list": [
                                                    {
                                                    "descriptor": {
                                                        "name": "Static Terms (Transaction Level)",
                                                        "code": "STATIC_TERMS"
                                                    },
                                                    "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                                    },
                                                    {
                                                    "descriptor": {
                                                        "name": "Offline Contract",
                                                        "code": "OFFLINE_CONTRACT"
                                                    },
                                                    "value": "true"
                                                    }
                                                ]
                                                }
                                            ]
                                            }
                                        }
                                        }
                    
                    # Send to gateway
                    request_body_str = json.dumps(payload, separators=(',', ':'))
                    auth_header = create_authorisation_header(request_body=request_body_str)

                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": auth_header,
                        "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                        "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
                    }

                    response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
                    return Response({
                            "status_code": response.status_code,
                            "response": response.json() if response.content else {}
                        }, status=status.HTTP_200_OK)      
                else:
                    return Response(
                        {"error": f"Form upload failed with status {res.status_code}"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
        except requests.exceptions.RequestException as e:
            return Response(
                {"error": f"Form upload failed: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": f"Unexpected error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
    )
        


class LumpINIT(APIView):

    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(SelectSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_init = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"


        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except(KeyError,TypeError):
            return Response(
                {"error": "Form URL not found in payload"},
                status=status.HTTP_400_BAD_REQUEST
            )

        payload={
                            "context": {
                                "location": {
                                "country": {
                                    "code": "IND"
                                },
                                "city": {
                                    "code": "*"
                                }
                                },
                                "domain": "ONDC:FIS14",
                                "timestamp": timestamp,
                               "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                                "transaction_id": transaction_id,
                                "message_id": message_id_init,
                                "version": "2.0.0",
                                "ttl": "PT10M",
                                "bpp_id": bpp_id,
                                "bpp_uri": bpp_uri,
                                "action": "init"
                            },
                            "message": {
                                "order": {
                                "provider": {
                                    "id": provider['id']
                                },
                                "items": [
                                    {
                                    "id": item[0]['id'],
                                    "quantity": {
                                        "selected": {
                                        "measure": {
                                            "value": "3000",
                                            "unit": "INR"
                                        }
                                        }
                                    },
                                    "fulfillment_ids": [
                                        item[0]['fulfillment_ids'][0]
                                    ]
                                    }
                                ],
                                "fulfillments": [
                                    {
                                    "id": fulfillments[0]['id'],
                                    "type": fulfillments[0]['type'],
                                    "customer": {
                                        "person": {
                                        "id": "pan:arrpp7771n",
                                        "creds": [
                                            {
                                            "id": "115.245.207.90",
                                            "type": "IP_ADDRESS"
                                            }
                                        ]
                                        },
                                        "contact": {
                                        "phone": "9916599123"
                                        }
                                    },
                                    "agent": {
                                        "person": {
                                        "id": "euin:E52432"
                                        },
                                        "organization": {
                                        "creds": [
                                            {
                                            "id": "ARN-124567",
                                            "type": "ARN"
                                            },
                                            {
                                            "id": "ARN-123456",
                                            "type": "SUB_BROKER_ARN"
                                            }
                                        ]
                                        }
                                    }
                                    }
                                ],
                                "payments": [
                                    {
                                    "collected_by": payments[0]['collected_by'],
                                    "params": {
                                        "amount": "3000",
                                        "currency": "INR",
                                        "source_bank_code": "icic0000047",
                                        "source_bank_account_number": "004701563111",
                                        "source_bank_account_name": "harish gupta"
                                    },
                                    "type": "PRE_FULFILLMENT",
                                    "tags": [
                                        {
                                        "descriptor": {
                                            "name": "Payment Method",
                                            "code": "PAYMENT_METHOD"
                                        },
                                        "list": [
                                            {
                                            "descriptor": {
                                                "code": "MODE"
                                            },
                                            "value": "NETBANKING"
                                            }
                                        ]
                                        }
                                    ]
                                    }
                                ],
                                "tags": [
                                    {
                                    "display": False,
                                    "descriptor": {
                                        "name": "BAP Terms of Engagement",
                                        "code": "BAP_TERMS"
                                    },
                                    "list": [
                                        {
                                        "descriptor": {
                                            "name": "Static Terms (Transaction Level)",
                                            "code": "STATIC_TERMS"
                                        },
                                        "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                        },
                                        {
                                        "descriptor": {
                                            "name": "Offline Contract",
                                            "code": "OFFLINE_CONTRACT"
                                        },
                                        "value": "true"
                                        }
                                    ]
                                    }
                                ]
                                }
                            }
                            }
        
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)                


class ConfirmLump(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnInitSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_conform = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            id=obj.payload['message']['order']['id']
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
            url=payments[0]['url']
        except (KeyError, TypeError) as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        label = None
        pay = obj.payload.get('message', {}).get('order', {}).get('payments', [])
        if pay:
            payment_url = pay[0].get('url', '')
            from urllib.parse import urlparse, parse_qs
            parsed_url = urlparse(payment_url)
            query_params = parse_qs(parsed_url.query)
            label = query_params.get('label', [None])[0]

        form_result = "APPROVED"
        payload = {"formResult": form_result}
        headers = {"Content-Type": "application/json"}
        url_1=f"{bpp_uri}/responseFromPaymentUtility?transaction_id={transaction_id}&label={label}"

        # try:
        response = requests.post(url_1, json=payload, headers=headers)
        print(response.status_code, response.text)
        logger.info(f"Status Code: {response.status_code}, Response: {response.text}")
        if response.status_code ==200:
            print(response)
            
            # if not payment_id:
            #     return Response({'error':"Missing Payment ID"},status=status.HTTP_400_BAD_REQUEST)
            # PaymentSubmisssion.objects.create(
            #         transaction=obj.transaction,
            #         payment_id=payment_id,
            #         message_id=message_id,
            #         timestamp=timestamp,
            #         status_pay=10
            #     )
            payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                       "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id,  
                        "message_id": message_id_conform,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri": bpp_uri,
                        "action": "confirm"
                    },
                    "message": {
                        "order": {
                        "id":id,
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ],
                            "payment_ids": [
                                item[0]['payment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type": fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n",
                                "creds": [
                                    {
                                    "id": "115.245.207.90",
                                    "type": "IP_ADDRESS"
                                    }
                                ]
                                },
                                "contact": {
                                "phone": "9916599123"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            }
                            }
                        ],
                        "payments": [
                            {
                            "id": payments[0]['id'],
                            "collected_by": payments[0]['collected_by'],
                            "status": payments[0]['status'],
                            "params": {
                                "amount": "3000",
                                "currency": "INR",
                                "source_bank_code": "icic0000047",
                                "source_bank_account_number": "004701563111",
                                "source_bank_account_name": "harish gupta"
                            },
                            "type": payments[0]['tags'][0]['list'][0]['value'],
                            "tags": [
                                {
                                "descriptor": {
                                    "name": "Payment Method",
                                    "code": "PAYMENT_METHOD"
                                },
                                "list": [
                                    {
                                    "descriptor": {
                                        "code": "MODE"
                                    },
                                    "value": "NETBANKING"
                                    }
                                ]
                                }
                            ]
                            }
                        ],
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            },
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BPP Terms of Engagement",
                                "code": "BPP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://sellerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)

            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }

            response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers) 
            return Response({
                    "status_code": response.status_code,
                    "response": response.json() if response.content else {}
                }, status=status.HTTP_200_OK)   

        else:
            error_detail = {
                "status_code": response.status_code,
                "response_text": response.text,  # raw text content
                "response_json": {},  # parsed JSON if possible
            }
            try:
                error_detail["response_json"] = response.json()
            except ValueError:
                # Response content is not JSON
                pass

            return Response(
                {
                    "error": "Payment Failed",
                    "details": error_detail
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        # except requests.exceptions.RequestException as e:
        #     return Response(
        #         {"error": f"Form upload failed: {str(e)}"},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )
        # except Exception as e:
        #     return Response(
        #         {"error": f"Unexpected error: {str(e)}"},
        #         status=status.HTTP_500_INTERNAL_SERVER_ERROR )



# Lumpsum With KYC New Folio


class LumpsumDigiLockerSubmission(APIView):

    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnStatus,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_select = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        
        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            xinput=obj.payload['message']['order']['xinput']
            fulfillments=obj.payload['message']['order']['fulfillments']
        except (KeyError, TypeError) as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
        )


        payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                        "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id, 
                        "message_id":message_id_select,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri": bpp_uri,
                        "action": "select"
                    },
                    "message": {
                        "order": {
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type":fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            }
                            }
                        ],
                        "xinput": {
                            "form": {
                            "id": xinput['form']['id']
                            },
                            "form_response": {
                            "submission_id": xinput['form_response']['submission_id']
                            }
                        },
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   


class LumpsumEsignFormSubmission(APIView):

    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id,bpp_id,bpp_uri,message_id]):
            return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
        
        obj=get_object_or_404(OnStatus,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
        message_id_select = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
                provider=obj.payload['message']['order']['provider']
                item=obj.payload['message']['order']['items']
                xinput=obj.payload['message']['order']['xinput']
                fulfillments=obj.payload['message']['order']['fulfillments']
        except (KeyError, TypeError) as e:
            return Response(
                    {"error": f"Missing key in payload: {e}"},
                    status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                       "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id, 
                        "message_id":message_id_select,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri":bpp_uri,
                        "action": "select"
                    },
                    "message": {
                        "order": {
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id":item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type": fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            }
                            }
                        ],
                        "xinput": {
                            "form": {
                            "id":xinput['form']['id']
                            },
                            "form_response": {
                            "submission_id": xinput['form_response']['submission_id']
                            }
                        },
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   
        


# Lumpsum Investment (Existing Folio - Investor selects/enters a folio)

class LumpsumExistingFolioInit(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            SelectSIP,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_init = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payments=obj.payload['message']['order']['payments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )

        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                "timestamp": timestamp,
                   "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,    
                    "message_id": message_id_init,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "init"
                },
                "message": {
                    "order": {
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            item[0]['fulfillment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type": fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n",
                            "creds": [
                                {
                                "id": "78953432/32",
                                "type": "FOLIO"
                                },
                                {
                                "id": "115.245.207.90",
                                "type": "IP_ADDRESS"
                                }
                            ]
                            },
                            "contact": {
                            "phone": "9916599123"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        }
                        }
                    ],
                    "payments": [
                        {
                        "collected_by":payments[0]['collected_by'],
                        "params": {
                            "amount": "3000",
                            "currency": "INR",
                            "source_bank_code": "icic0000047",
                            "source_bank_account_number": "004701563111",
                            "source_bank_account_name": "harish gupta"
                        },
                        "type": payments[0]['type'],
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Payment Method",
                                "code": "PAYMENT_METHOD"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "code": "MODE"
                                },
                                "value": "NETBANKING"
                                }
                            ]
                            }
                        ]
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
        
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers) 
        return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)   

            

class LumpConfirmExisting(APIView):
        def post(self,request,*args,**kwargs):
            transaction_id=request.data.get('transaction_id')
            bpp_id = request.data.get('bpp_id')
            bpp_uri = request.data.get('bpp_uri')
            message_id=request.data.get('message_id')

            if not all([transaction_id,bpp_id,bpp_uri,message_id]):
                return Response({"error":"Required all Fields"},status=status.HTTP_400_BAD_REQUEST)
            
            obj=get_object_or_404(OnInitSIP,payload__context__bpp_id=bpp_id,payload__context__bpp_uri=bpp_uri,transaction__transaction_id=transaction_id,payload__context__message_id=message_id)
            message_id_conform = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

            try:
                id=obj.payload['message']['order']['id']
                provider=obj.payload['message']['order']['provider']
                item=obj.payload['message']['order']['items']
                fulfillments=obj.payload['message']['order']['fulfillments']
                payments=obj.payload['message']['order']['payments']
                url=payments[0]['url']
            except (KeyError, TypeError) as e:
                return Response(
                    {"error": f"Missing key in payload: {e}"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                        "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id,  
                        "message_id": message_id_conform,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri":bpp_uri,
                        "action": "confirm"
                    },
                    "message": {
                        "order": {
                        "id": id,
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ],
                            "payment_ids": [
                                item[0]['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type": fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n",
                                "creds": [
                                    {
                                    "id": "78953432/32",
                                    "type": "FOLIO"
                                    },
                                    {
                                    "id": "115.245.207.90",
                                    "type": "IP_ADDRESS"
                                    }
                                ]
                                },
                                "contact": {
                                "phone": "9916599123"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            }
                            }
                        ],
                        "payments": [
                            {
                            "id": payments[0]['id'],
                            "collected_by": payments[0]['collected_by'],
                            "status": payments[0]['status'],
                            "params": {
                                "amount": "3000",
                                "currency": "INR",
                                "source_bank_code": "icic0000047",
                                "source_bank_account_number": "004701563111",
                                "source_bank_account_name": "harish gupta"
                            },
                            "type": "PRE_FULFILLMENT",
                            "tags": [
                                {
                                "descriptor": {
                                    "name": "Payment Method",
                                    "code": "PAYMENT_METHOD"
                                },
                                "list": [
                                    {
                                    "descriptor": {
                                        "code": "MODE"
                                    },
                                    "value": "NETBANKING"
                                    }
                                ]
                                }
                            ]
                            }
                        ],
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            },
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BPP Terms of Engagement",
                                "code": "BPP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://sellerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)

            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }

            response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers) 
            return Response({
                    "status_code": response.status_code,
                    "response": response.json() if response.content else {}
                }, status=status.HTTP_200_OK)   

# Lumpsum Payment Retry

class LumpRetryInit(APIView):
        def post(self,request,*args,**kwargs):
            transaction_id=request.data.get('transaction_id')
            bpp_id = request.data.get('bpp_id')
            bpp_uri = request.data.get('bpp_uri')
            message_id=request.data.get('message_id')

            if not all([transaction_id, bpp_id, bpp_uri,message_id]):
                return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                            status=status.HTTP_400_BAD_REQUEST)
            
            obj = get_object_or_404(
                SelectSIP,
                payload__context__bpp_id=bpp_id,
                payload__context__bpp_uri=bpp_uri,
                transaction__transaction_id=transaction_id
            )
            
            message_id_init = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

            try:
                provider=obj.payload['message']['order']['provider']
                item=obj.payload['message']['order']['items']
                fulfillments=obj.payload['message']['order']['fulfillments']
                payment=obj.payload['message']['order']['payments']
            except KeyError as e:
                return Response(
                    {"error": f"Missing key in payload: {e}"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except TypeError:
                return Response(
                    {"error": "Invalid payload structure (possibly None or wrong type)"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            payload={
                    "context": {
                        "location": {
                        "country": {
                            "code": "IND"
                        },
                        "city": {
                            "code": "*"
                        }
                        },
                        "domain": "ONDC:FIS14",
                        "timestamp": timestamp,
                       "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                        "transaction_id": transaction_id,  
                        "message_id": message_id_init,
                        "version": "2.0.0",
                        "ttl": "PT10M",
                        "bpp_id": bpp_id,
                        "bpp_uri": bpp_uri,
                        "action": "init"
                    },
                    "message": {
                        "order": {
                        "provider": {
                            "id": provider['id']
                        },
                        "items": [
                            {
                            "id": item[0]['id'],
                            "quantity": {
                                "selected": {
                                "measure": {
                                    "value": "3000",
                                    "unit": "INR"
                                }
                                }
                            },
                            "fulfillment_ids": [
                                obj.payload['message']['order']['quote']['breakup'][0]['item']['fulfillment_ids'][0]
                            ]
                            }
                        ],
                        "fulfillments": [
                            {
                            "id": fulfillments[0]['id'],
                            "type": fulfillments[0]['type'],
                            "customer": {
                                "person": {
                                "id": "pan:arrpp7771n",
                                "creds": [
                                    {
                                    "id": "78953432/32",
                                    "type": "FOLIO"
                                    },
                                    {
                                    "id": "115.245.207.90",
                                    "type": "IP_ADDRESS"
                                    }
                                ]
                                },
                                "contact": {
                                "phone": "9916599123"
                                }
                            },
                            "agent": {
                                "person": {
                                "id": "euin:E52432"
                                },
                                "organization": {
                                "creds": [
                                    {
                                    "id": "ARN-124567",
                                    "type": "ARN"
                                    },
                                    {
                                    "id": "ARN-123456",
                                    "type": "SUB_BROKER_ARN"
                                    }
                                ]
                                }
                            }
                            }
                        ],
                        "payments": [
                            {
                            "collected_by": payment[0]['collected_by'],
                            "params": {
                                "amount": "3000",
                                "currency": "INR",
                                "source_bank_code": "icic0000047",
                                "source_bank_account_number": "004701563111",
                                "source_bank_account_name": "harish gupta"
                            },
                            "type": payment[0]['type'],
                            "tags": [
                                {
                                "descriptor": {
                                    "name": "Payment Method",
                                    "code": "PAYMENT_METHOD"
                                },
                                "list": [
                                    {
                                    "descriptor": {
                                        "code": "MODE"
                                    },
                                    "value": "NETBANKING"
                                    }
                                ]
                                }
                            ]
                            }
                        ],
                        "tags": [
                            {
                            "display": False,
                            "descriptor": {
                                "name": "BAP Terms of Engagement",
                                "code": "BAP_TERMS"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Static Terms (Transaction Level)",
                                    "code": "STATIC_TERMS"
                                },
                                "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                "descriptor": {
                                    "name": "Offline Contract",
                                    "code": "OFFLINE_CONTRACT"
                                },
                                "value": "true"
                                }
                            ]
                            }
                        ]
                        }
                    }
                    }
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)

            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }

            response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers)

            return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)    


class LumpRetryConfirm(APIView):
     def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                        status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            OnInitSIP,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_confirm = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payment=obj.payload['message']['order']['payments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )

        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,  
                    "message_id":message_id_confirm,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "confirm"
                },
                "message": {
                    "order": {
                    "id": obj.payload['message']['order']['id'],
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            obj.payload['message']['order']['quote']['breakup'][0]['item']['fulfillment_ids'][0]
                        ],
                        "payment_ids": [
                            item[0]['payment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type": fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n",
                            "creds": [
                                {
                                "id": "78953432/32",
                                "type": "FOLIO"
                                },
                                {
                                "id": "115.245.207.90",
                                "type": "IP_ADDRESS"
                                }
                            ]
                            },
                            "contact": {
                            "phone": "9916599123"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        }
                        }
                    ],
                    "payments": [
                        {
                        "id": payment[0]['id'],
                        "collected_by": payment[0]['collected_by'],
                        "status": payment[0]['status'],
                        "params": {
                            "amount": "3000",
                            "currency": "INR",
                            "source_bank_code": "icic0000047",
                            "source_bank_account_number": "004701563111",
                            "source_bank_account_name": "harish gupta"
                        },
                        "type": payment[0]['type'],
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Payment Method",
                                "code": "PAYMENT_METHOD"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "code": "MODE"
                                },
                                "value": "NETBANKING"
                                }
                            ]
                            }
                        ]
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        },
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BPP Terms of Engagement",
                            "code": "BPP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://sellerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }    
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK) 


class LumpRetryUpdate(APIView):
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                        status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            OnConfirm,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_update = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
            payment=obj.payload['message']['order']['payments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,  
                    "message_id": message_id_update,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri":bpp_uri,
                    "action": "update"
                },
                "message": {
                    "update_target": 'order.payments',
                    "order": {
                    "id": obj.payload['message']['order']['id'],
                    "payments": [
                        {
                        "collected_by": payment[0]['collected_by'],
                        "params": {
                            "amount": "3000",
                            "currency": "INR",
                            "source_bank_code": "icic0000047",
                            "source_bank_account_number": "004701563111",
                            "source_bank_account_name": "harish gupta"
                        },
                        "type": payment[0]['type'],
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Payment Method",
                                "code": "PAYMENT_METHOD"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "code": "MODE"
                                },
                                "value": "UPI_URI"
                                }
                            ]
                            }
                        ]
                        }
                    ]
                    }
                }
                }

        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/update", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)    

# Redemption

class RedemptionSelect(APIView):
        def post(self, request, *args, **kwargs):
            transaction_id = request.data.get('transaction_id')
            bpp_id = request.data.get('bpp_id')
            bpp_uri = request.data.get('bpp_uri')
            preferred_type=request.data.get('preferred_type')

            if not all([transaction_id, bpp_id, bpp_uri,preferred_type]):
                return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                            status=status.HTTP_400_BAD_REQUEST)
            
            obj = get_object_or_404(
                FullOnSearch,
                payload__context__bpp_id=bpp_id,
                payload__context__bpp_uri=bpp_uri,
                transaction__transaction_id=transaction_id
            )
            
            message_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
            print(obj.payload)

            # Get the first provider and item
            provider = obj.payload["message"]["catalog"]["providers"][0]
            catalog = obj.payload["message"]["catalog"]
            fulfillment_type_map = {f["id"]: f["type"] for f in provider.get("fulfillments", [])}
            matching_fulfillment_id = next(
                (fid for item in provider.get("items", [])
                for fid in item.get("fulfillment_ids", [])
                if fulfillment_type_map.get(fid) == preferred_type),
                None
            )
            matching_fulfillment = next((f for f in provider["fulfillments"] if f.get("type") == preferred_type),None)

            if not matching_fulfillment:
                return Response({"error": f"No fulfillment with type '{preferred_type}' found."},
                                status=status.HTTP_404_NOT_FOUND)



            payload = {
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,  
                    "message_id": message_id,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "select"
                },
                "message": {
                    "order": {
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": provider['items'][0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                            matching_fulfillment_id
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": matching_fulfillment['id'],
                        "type":matching_fulfillment['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n",
                            "creds": [
                                {
                                "id": "78953432/32",
                                "type": "FOLIO"
                                }
                            ]
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        }
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)

            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }

            response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers)

            return Response({
                "status_code": response.status_code,
                "response": response.json() if response.content else {}
            }, status=status.HTTP_200_OK)    




class RedemptionInit(APIView):
    def post(self,request,*args,**kwargs):

        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            SelectSIP,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_init = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
            "context": {
                "location": {
                "country": {
                    "code": "IND"
                },
                "city": {
                    "code": "*"
                }
                },
                "domain": "ONDC:FIS14",
                "timestamp": timestamp,
               "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                "transaction_id": transaction_id,  
                "message_id": message_id_init,
                "version": "2.0.0",
                "ttl": "PT10M",
                "bpp_id":bpp_id,
                "bpp_uri": bpp_uri,
                "action": "init"
            },
            "message": {
                "order": {
                "provider": {
                    "id": provider['id']
                },
                "items": [
                    {
                    "id": item[0]['id'],
                    "quantity": {
                        "selected": {
                        "measure": {
                            "value": "3000",
                            "unit": "INR"
                        }
                        }
                    },
                    "fulfillment_ids": [
                        item[0]['fulfillment_ids'][0]
                    ]
                    }
                ],
                "fulfillments": [
                    {
                    "id": fulfillments[0]['id'],
                    "type": fulfillments[0]['type'],
                    "customer": {
                        "person": {
                        "id": "pan:arrpp7771n",
                        "creds": [
                            {
                            "id": "78953432/32",
                            "type": "FOLIO"
                            },
                            {
                            "id": "115.245.207.90",
                            "type": "IP_ADDRESS"
                            }
                        ]
                        },
                        "contact": {
                        "phone": "9916599123"
                        }
                    },
                    "agent": {
                        "person": {
                        "id": "euin:E52432"
                        },
                        "organization": {
                        "creds": [
                            {
                            "id": "ARN-124567",
                            "type": "ARN"
                            },
                            {
                            "id": "ARN-123456",
                            "type": "SUB_BROKER_ARN"
                            }
                        ]
                        }
                    },
                    "tags": [
                        {
                        "descriptor": {
                            "name": "Payout Bank Account",
                            "code": "PAYOUT_BANK_ACCOUNT"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Identifier",
                                "code": "IDENTIFIER"
                            },
                            "value": "ba_369823523564"
                            }
                        ]
                        }
                    ]
                    }
                ],
                "tags": [
                    {
                    "display": False,
                    "descriptor": {
                        "name": "BAP Terms of Engagement",
                        "code": "BAP_TERMS"
                    },
                    "list": [
                        {
                        "descriptor": {
                            "name": "Static Terms (Transaction Level)",
                            "code": "STATIC_TERMS"
                        },
                        "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                        },
                        {
                        "descriptor": {
                            "name": "Offline Contract",
                            "code": "OFFLINE_CONTRACT"
                        },
                        "value": "true"
                        }
                    ]
                    }
                ]
                }
            }
            }
        
         # Send to gateway
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/init", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)    




            
class RedemptionConfirm(APIView):
        
    def post(self,request,*args,**kwargs):
        transaction_id=request.data.get('transaction_id')
        bpp_id = request.data.get('bpp_id')
        bpp_uri = request.data.get('bpp_uri')
        message_id=request.data.get('message_id')

        if not all([transaction_id, bpp_id, bpp_uri,message_id]):
            return Response({"error": "Missing transaction_id, bpp_id, or bpp_uri"}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        obj = get_object_or_404(
            OnInitSIP,
            payload__context__bpp_id=bpp_id,
            payload__context__bpp_uri=bpp_uri,
            transaction__transaction_id=transaction_id
        )
        
        message_id_confirm = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"

        try:
            provider=obj.payload['message']['order']['provider']
            item=obj.payload['message']['order']['items']
            fulfillments=obj.payload['message']['order']['fulfillments']
        except KeyError as e:
            return Response(
                {"error": f"Missing key in payload: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except TypeError:
            return Response(
                {"error": "Invalid payload structure (possibly None or wrong type)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payload={
                "context": {
                    "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                    },
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id_confirm,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "confirm"
                },
                "message": {
                    "order": {
                    "id": obj.payload['message']['order']['id'],
                    "provider": {
                        "id": provider['id']
                    },
                    "items": [
                        {
                        "id": item[0]['id'],
                        "quantity": {
                            "selected": {
                            "measure": {
                                "value": "3000",
                                "unit": "INR"
                            }
                            }
                        },
                        "fulfillment_ids": [
                        item[0]['fulfillment_ids'][0]
                        ]
                        }
                    ],
                    "fulfillments": [
                        {
                        "id": fulfillments[0]['id'],
                        "type": fulfillments[0]['type'],
                        "customer": {
                            "person": {
                            "id": "pan:arrpp7771n",
                            "creds": [
                                {
                                "id": "78953432/32",
                                "type": "FOLIO"
                                },
                                {
                                "id": "115.245.207.90",
                                "type": "IP_ADDRESS"
                                }
                            ]
                            },
                            "contact": {
                            "phone": "9916599123"
                            }
                        },
                        "agent": {
                            "person": {
                            "id": "euin:E52432"
                            },
                            "organization": {
                            "creds": [
                                {
                                "id": "ARN-124567",
                                "type": "ARN"
                                },
                                {
                                "id": "ARN-123456",
                                "type": "SUB_BROKER_ARN"
                                }
                            ]
                            }
                        },
                        "tags": [
                            {
                            "descriptor": {
                                "name": "Payout Bank Account",
                                "code": "PAYOUT_BANK_ACCOUNT"
                            },
                            "list": [
                                {
                                "descriptor": {
                                    "name": "Identifier",
                                    "code": "IDENTIFIER"
                                },
                                "value": "ba_369823523564"
                                }
                            ]
                            }
                        ]
                        }
                    ],
                    "tags": [
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BAP Terms of Engagement",
                            "code": "BAP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        },
                        {
                        "display": False,
                        "descriptor": {
                            "name": "BPP Terms of Engagement",
                            "code": "BPP_TERMS"
                        },
                        "list": [
                            {
                            "descriptor": {
                                "name": "Static Terms (Transaction Level)",
                                "code": "STATIC_TERMS"
                            },
                            "value": "https://sellerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                            },
                            {
                            "descriptor": {
                                "name": "Offline Contract",
                                "code": "OFFLINE_CONTRACT"
                            },
                            "value": "true"
                            }
                        ]
                        }
                    ]
                    }
                }
                }
        
        request_body_str = json.dumps(payload, separators=(',', ':'))
        auth_header = create_authorisation_header(request_body=request_body_str)

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
            "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
        }

        response = requests.post(f"{bpp_uri}/confirm", data=request_body_str, headers=headers)

        return Response({
            "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }, status=status.HTTP_200_OK)    








# For testing Only

# views.py - Add this new view to orchestrate the complete flow

import asyncio
import time
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)

class CompleteSIPFlowView(APIView):
    """
    Orchestrates the complete SIP creation flow with a single API call
    """
    
    def post(self, request, *args, **kwargs):
        preferred_type = request.data.get('preferred_type', 'SIP')
        
        try:
            # Step 1: Search
            search_result = self._execute_search()
            if not search_result['success']:
                return Response(search_result, status=status.HTTP_400_BAD_REQUEST)
            
            transaction_id = search_result['transaction_id']
            
            # Step 2: Wait for on_search callback (with timeout)
            on_search_data = self._wait_for_on_search(transaction_id)
            if not on_search_data:
                return Response({
                    'success': False, 
                    'error': 'Timeout waiting for search results'
                }, status=status.HTTP_408_REQUEST_TIMEOUT)
            
            # Step 3: Select SIP
            select_result = self._execute_select(
                transaction_id, 
                on_search_data['bpp_id'], 
                on_search_data['bpp_uri'], 
                preferred_type
            )
            if not select_result['success']:
                return Response(select_result, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 4: Wait for on_select callback
            on_select_data = self._wait_for_on_select(transaction_id)
            if not on_select_data:
                return Response({
                    'success': False, 
                    'error': 'Timeout waiting for select confirmation'
                }, status=status.HTTP_408_REQUEST_TIMEOUT)
            
            # Step 5: Submit Form (if required)
            form_result = self._execute_form_submission(
                transaction_id, 
                on_select_data['bpp_id'], 
                on_select_data['bpp_uri']
            )
            if not form_result['success']:
                return Response(form_result, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 6: Initialize
            init_result = self._execute_init(
                transaction_id, 
                on_select_data['bpp_id'], 
                on_select_data['bpp_uri'],
                form_result['message_id']
            )
            if not init_result['success']:
                return Response(init_result, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 7: Wait for on_init callback
            on_init_data = self._wait_for_on_init(transaction_id)
            if not on_init_data:
                return Response({
                    'success': False, 
                    'error': 'Timeout waiting for init confirmation'
                }, status=status.HTTP_408_REQUEST_TIMEOUT)
            
            # Step 8: Confirm SIP
            confirm_result = self._execute_confirm(
                transaction_id, 
                on_init_data['bpp_id'], 
                on_init_data['bpp_uri'],
                on_init_data['message_id']
            )
            if not confirm_result['success']:
                return Response(confirm_result, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 9: Wait for final on_confirm callback
            on_confirm_data = self._wait_for_on_confirm(transaction_id)
            if not on_confirm_data:
                return Response({
                    'success': False, 
                    'error': 'Timeout waiting for final confirmation'
                }, status=status.HTTP_408_REQUEST_TIMEOUT)
            
            return Response({
                'success': True,
                'message': 'SIP created successfully',
                'transaction_id': transaction_id,
                'order_details': on_confirm_data.get('order_details'),
                'flow_summary': {
                    'search_completed': True,
                    'select_completed': True,
                    'form_submitted': True,
                    'init_completed': True,
                    'confirm_completed': True
                }
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Complete SIP flow failed: {str(e)}", exc_info=True)
            return Response({
                'success': False,
                'error': f'Flow execution failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _execute_search(self):
        """Execute the search step"""
        try:
            transaction_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
            
            payload = {
                "context": {
                    "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                    "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "action": "search"
                },
                "message": {
                    "intent": {
                        "category": {"descriptor": {"code": "MUTUAL_FUNDS"}},
                        "fulfillment": {
                            "agent": {
                                "organization": {
                                    "creds": [{"id": "ARN-125784", "type": "ARN"}]
                                }
                            }
                        },
                        "tags": [{
                            "display": False,
                            "descriptor": {"name": "BAP Terms of Engagement", "code": "BAP_TERMS"},
                            "list": [
                                {
                                    "descriptor": {"name": "Static Terms (Transaction Level)", "code": "STATIC_TERMS"},
                                    "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                    "descriptor": {"name": "Offline Contract", "code": "OFFLINE_CONTRACT"},
                                    "value": "true"
                                }
                            ]
                        }]
                    }
                }
            }
            
            # Store transaction
            transaction, _ = Transaction.objects.get_or_create(transaction_id=transaction_id)
            Message.objects.create(
                transaction=transaction,
                message_id=message_id,
                action="search",
                timestamp=parse_datetime(timestamp),
                payload=payload
            )
            
            # Send request
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }
            
            response = requests.post("https://preprod.gateway.ondc.org/search", 
                                   data=request_body_str, headers=headers)
            
            if response.status_code == 200:
                return {
                    'success': True,
                    'transaction_id': transaction_id,
                    'message_id': message_id
                }
            else:
                return {
                    'success': False,
                    'error': f'Search request failed with status {response.status_code}'
                }
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _wait_for_on_search(self, transaction_id, timeout=30):
        """Wait for on_search callback with timeout"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
                search_entry = FullOnSearch.objects.filter(transaction=transaction).first()
                if search_entry:
                    payload = search_entry.payload
                    return {
                        'bpp_id': payload['context']['bpp_id'],
                        'bpp_uri': payload['context']['bpp_uri'],
                        'payload': payload
                    }
            except:
                pass
            time.sleep(2)  # Wait 2 seconds before checking again
        return None
    
    def _execute_select(self, transaction_id, bpp_id, bpp_uri, preferred_type):
        """Execute the select step"""
        try:
            obj = FullOnSearch.objects.get(
                payload__context__bpp_id=bpp_id,
                payload__context__bpp_uri=bpp_uri,
                transaction__transaction_id=transaction_id
            )
            
            message_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
            
            provider = obj.payload["message"]["catalog"]["providers"][0]
            matching_fulfillment = next(
                (f for f in provider["fulfillments"] if f.get("type") == preferred_type), None
            )
            
            if not matching_fulfillment:
                return {'success': False, 'error': f'No fulfillment with type {preferred_type} found'}
            
            # Build select payload (similar to your existing SIPCreationView)
            payload = {
                "context": {
                    "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
                    "domain": "ONDC:FIS14",
                    "timestamp": timestamp,
                    "bap_id": "investment.preprod.vyable.in",
                    "bap_uri": "https://investment.preprod.vyable.in/ondc",
                    "transaction_id": transaction_id,
                    "message_id": message_id,
                    "version": "2.0.0",
                    "ttl": "PT10M",
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "action": "select"
                },
                "message": {
                    "order": {
                        "provider": {"id": provider['id']},
                        "items": [{
                            "id": provider['items'][0]['id'],
                            "quantity": {
                                "selected": {
                                    "measure": {"value": "3000", "unit": "INR"}
                                }
                            }
                        }],
                        "fulfillments": [{
                            "id": matching_fulfillment['id'],
                            "type": matching_fulfillment['type'],
                            "customer": {"person": {"id": "pan:arrpp7771n"}},
                            "agent": {
                                "person": {"id": "euin:E52432"},
                                "organization": {
                                    "creds": [
                                        {"id": "ARN-124567", "type": "ARN"},
                                        {"id": "ARN-123456", "type": "SUB_BROKER_ARN"}
                                    ]
                                }
                            },
                            "stops": [{
                                "time": {
                                    "schedule": {
                                        "frequency": matching_fulfillment["tags"][0]["list"][0]["value"]
                                    }
                                }
                            }]
                        }],
                        "tags": [{
                            "display": False,
                            "descriptor": {"name": "BAP Terms of Engagement", "code": "BAP_TERMS"},
                            "list": [
                                {
                                    "descriptor": {"name": "Static Terms (Transaction Level)", "code": "STATIC_TERMS"},
                                    "value": "https://buyerapp.com/legal/ondc:fis14/static_terms?v=0.1"
                                },
                                {
                                    "descriptor": {"name": "Offline Contract", "code": "OFFLINE_CONTRACT"},
                                    "value": "true"
                                }
                            ]
                        }]
                    }
                }
            }
            
            # Store and send
            transaction = Transaction.objects.get(transaction_id=transaction_id)
            Message.objects.create(
                transaction=transaction,
                message_id=message_id,
                action="select",
                timestamp=parse_datetime(timestamp),
                payload=payload
            )
            
            request_body_str = json.dumps(payload, separators=(',', ':'))
            auth_header = create_authorisation_header(request_body=request_body_str)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header,
                "X-Gateway-Authorization": os.getenv("SIGNED_UNIQUE_REQ_ID", ""),
                "X-Gateway-Subscriber-Id": os.getenv("SUBSCRIBER_ID")
            }
            
            response = requests.post(f"{bpp_uri}/select", data=request_body_str, headers=headers)
            
            if response.status_code == 200:
                return {'success': True, 'message_id': message_id}
            else:
                return {'success': False, 'error': f'Select request failed with status {response.status_code}'}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _wait_for_on_select(self, transaction_id, timeout=30):
        """Wait for on_select callback"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
                select_entry = SelectSIP.objects.filter(transaction=transaction).first()
                if select_entry:
                    payload = select_entry.payload
                    return {
                        'bpp_id': payload['context']['bpp_id'],
                        'bpp_uri': payload['context']['bpp_uri'],
                        'message_id': payload['context']['message_id'],
                        'payload': payload
                    }
            except:
                pass
            time.sleep(2)
        return None
    
    def _execute_form_submission(self, transaction_id, bpp_id, bpp_uri):
        """Execute form submission step"""
        try:
            obj = SelectSIP.objects.get(
                payload__context__bpp_id=bpp_id,
                payload__context__bpp_uri=bpp_uri,
                transaction__transaction_id=transaction_id
            )
            
            message_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat(sep="T", timespec="milliseconds") + "Z"
            
            # Extract form URL
            xinput = obj.payload["message"]["order"]["xinput"]
            url = xinput["form"]["url"]
            
            # Submit KYC data
            user_kyc_data = {
                "pan": "ABCDE1234F",
                "dob": "1990-01-01",
                "email": "user@example.com",
                "name": "Ravi Kumar",
                "gender": "Male",
                "marital_status": "Married",
                "occupation": "Salaried",
                "source_of_wealth": "Business",
                "income_range": "1L to 5L",
                "cob": "India",
                "pob": "Kochi",
                "political_exposure": "no_exposure",
                "india_tax_residency_status": "resident",
                "mode_of_holding": "single",
                "ca_line": "hfjfk jifl jffj"
            }
            
            res = requests.post(url, json=user_kyc_data)
            if res.status_code != 200:
                return {'success': False, 'error': f'Form submission failed with status {res.status_code}'}
            
            resp_json = res.json()
            submission_id = resp_json.get('submission_id')
            if not submission_id:
                return {'success': False, 'error': 'Submission ID missing from form response'}
            
            # Store submission ID
            SubmissionID.objects.create(
                transaction=obj.transaction,
                submission_id=submission_id,
                message_id=message_id,
                timestamp=timestamp
            )
            
            # Continue with the rest of form submission logic...
            # (Include the full payload construction from your FormSubmisssion view)
            
            return {'success': True, 'message_id': message_id, 'submission_id': submission_id}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _execute_init(self, transaction_id, bpp_id, bpp_uri, message_id):
        """Execute init step"""
        # Implementation similar to your INIT view
        try:
            # Your existing INIT logic here
            return {'success': True, 'message_id': str(uuid.uuid4())}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _wait_for_on_init(self, transaction_id, timeout=30):
        """Wait for on_init callback"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
                init_entry = OnInitSIP.objects.filter(transaction=transaction).first()
                if init_entry:
                    payload = init_entry.payload
                    return {
                        'bpp_id': payload['context']['bpp_id'],
                        'bpp_uri': payload['context']['bpp_uri'],
                        'message_id': payload['context']['message_id'],
                        'payload': payload
                    }
            except:
                pass
            time.sleep(2)
        return None
    
    def _execute_confirm(self, transaction_id, bpp_id, bpp_uri, message_id):
        """Execute confirm step"""
        # Implementation similar to your ConfirmSIP view
        try:
            # Your existing ConfirmSIP logic here
            return {'success': True, 'message_id': str(uuid.uuid4())}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _wait_for_on_confirm(self, transaction_id, timeout=30):
        """Wait for on_confirm callback"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                transaction = Transaction.objects.get(transaction_id=transaction_id)
                confirm_entry = OnConfirm.objects.filter(transaction=transaction).first()
                if confirm_entry:
                    return {
                        'order_details': confirm_entry.payload.get('message', {}).get('order', {}),
                        'payload': confirm_entry.payload
                    }
            except:
                pass
            time.sleep(2)
        return None


