#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
# vim: tabstop=2 shiftwidth=2 softtabstop=2 expandtab

from datetime import datetime
import time
import logging
import io
import os
import json
import requests
from bs4 import BeautifulSoup
import arrow

import boto3

LOGGER = logging.getLogger()
if len(LOGGER.handlers) > 0:
  # The Lambda environment pre-configures a handler logging to stderr.
  # If a handler is already configured, `.basicConfig` does not execute.
  # Thus we set the level directly.
  LOGGER.setLevel(logging.INFO)
else:
  logging.basicConfig(level=logging.INFO)

DRY_RUN = True if 'true' == os.getenv('DRY_RUN', 'true') else False

AWS_REGION = os.getenv('REGION_NAME', 'us-east-1')

S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME')
S3_OBJ_KEY_PREFIX = os.getenv('S3_OBJ_KEY_PREFIX', 'posts')

EMAIL_FROM_ADDRESS = os.getenv('EMAIL_FROM_ADDRESS')
EMAIL_TO_ADDRESSES = os.getenv('EMAIL_TO_ADDRESSES')
EMAIL_TO_ADDRESSES = [e.strip() for e in EMAIL_TO_ADDRESSES.split(',')]

TRANS_DEST_LANG = os.getenv('TRANS_DEST_LANG', 'ko')

MAX_SINGLE_TEXT_SIZE = 15*1204

TRANS_CLIENT = None

WEBHOOK_URL = os.environ['webHookUrl']
SLACK_CHANNEL = os.environ['slackChannel']

def send_message_to_slack(attachment):

    url = WEBHOOK_URL
    payload = { 
        'channel': SLACK_CHANNEL,
        'text' : '',
        'attachments' : attachment,
        'mrkdwn': "true",
        'icon_url': 'http://dsbrrxis5yatj.cloudfront.net/aws.png'
    } 
    requests.post(url, json = payload)
    
def fwrite_s3(s3_client, doc, s3_bucket, s3_obj_key):
  output = io.StringIO()
  output.write(doc)

  ret = s3_client.put_object(Body=output.getvalue(),
    Bucket=s3_bucket,
    Key=s3_obj_key)

  output.close()
  try:
    status_code = ret['ResponseMetadata']['HTTPStatusCode']
    return (200 == status_code)
  except Exception as ex:
    return False


def gen_html(elem):
  HTML_FORMAT = '''<!DOCTYPE html>
<html>
<head>
<style>
table {{
  font-family: arial, sans-serif;
  border-collapse: collapse;
  width: 100%;
}}
td, th {{
  border: 1px solid #dddddd;
  text-align: left;
  padding: 8px;
}}
tr:nth-child(even) {{
  background-color: #dddddd;
}}
</style>
</head>
<body>
<h2>{title}</h2>
<table>
  <tr>
    <th>key</th>
    <th>value</th>
  </tr>
  <tr>
    <td>doc_id</th>
    <td>{doc_id}</td>
  </tr>
  <tr>
    <td>link</th>
    <td>{link}</td>
  </tr>
  <tr>
    <td>pub_date</th>
    <td>{pub_date}</td>
  </tr>
  <tr>
    <td>section</th>
    <td>{section}</td>
  </tr>
  <tr>
    <td>title_{lang}</th>
    <td>{title_trans}</td>
  </tr>
  <tr>
    <td>body_{lang}</th>
    <td>{body_trans}</td>
  </tr>
  <tr>
    <td>tags</th>
    <td>{tags}</td>
  </tr>
</table>
</body>
</html>'''


  html_doc = HTML_FORMAT.format(title=elem['title'],
    doc_id=elem['doc_id'],
    link=elem['link'],
    pub_date=elem['pub_date'],
    section=elem['section'],
    title_trans=elem['title_trans'],
    body_trans='<br/>'.join([e for e in elem['body_trans']]),
    tags=elem['tags'],
    lang=elem['lang'])

  return html_doc


def send_email(ses_client, from_addr, to_addrs, subject, html_body):
  ret = ses_client.send_email(Destination={'ToAddresses': to_addrs},
    Message={'Body': {
        'Html': {
          'Charset': 'UTF-8',
          'Data': html_body
        }
      },
      'Subject': {
        'Charset': 'UTF-8',
        'Data': subject
      }
    },
    Source=from_addr
  )
  return ret


def get_or_create_translator(region_name):
  global TRANS_CLIENT

  if not TRANS_CLIENT:
    TRANS_CLIENT = boto3.client('translate', region_name=region_name)
  assert TRANS_CLIENT
  return TRANS_CLIENT


def translate(translator, text, src='en', dest='ko'):
  trans_res = translator.translate_text(Text=text,
    SourceLanguageCode=src, TargetLanguageCode=dest)
  trans_text = trans_res['TranslatedText'] if 200 == trans_res['ResponseMetadata']['HTTPStatusCode'] else None
  return trans_text


def lambda_handler(event, context):
  
  LOGGER.debug('receive SNS message')

  s3_client = boto3.client('s3', region_name=AWS_REGION)
  ses_client = boto3.client('ses', region_name=AWS_REGION)

  for record in event['Records']:
    msg = json.loads(record['Sns']['Message'])
    LOGGER.debug('message: %s' % json.dumps(msg))

    doc_id = msg['id']
    url = msg['link']
    
    print (url)
    res = requests.get(url)
    html = res.text
    soup = BeautifulSoup(html, 'html.parser')
        
    article = soup.find("article", class_="blog-post")
    
    section_tag = soup.find("meta", property="article:section")
    section = section_tag["content"].strip() if section_tag != None else ""
        
    tag_tag = article.find("span", class_="blog-post-categories")    
    tag = tag_tag.text.strip() if tag_tag != None else ""
        
    pub_date_tag = article.find("time", property="datePublished")
    published_time = pub_date_tag["datetime"].strip() if pub_date_tag != None else ""
    
    title_tag = article.find("h1", class_="lb-h2 blog-post-title", property="name headline")    
    title = title_tag.text.strip() if title_tag != None else ""
    
    body_tag = article.find("section", class_="blog-post-content", property="articleBody")    
    body_text = body_tag.text.strip() if body_tag != None else ""
        
    #XX: https://py-googletrans.readthedocs.io/en/latest/
    # assert len(body_text) < MAX_SINGLE_TEXT_SIZE

    translator = get_or_create_translator(region_name=AWS_REGION)
    trans_title = translate(translator, title, dest=TRANS_DEST_LANG)

    sentences = [e for e in body_text.split('\n') if e]
    trans_body_texts = []
    for sentence in sentences:
      trans_sentence = translate(translator, sentence, dest=TRANS_DEST_LANG)
      trans_body_texts.append(trans_sentence)

    doc = {
      'doc_id': doc_id,
      'link': url,
      'lang': TRANS_DEST_LANG,
      'pub_date': published_time,
      'section': section,
      'title': title,
      'title_trans': trans_title,
      'body_trans': trans_body_texts,
      'tags': tag
    }
    html = gen_html(doc)
    if not DRY_RUN:
      subject = '''[translated] {title}'''.format(title=doc['title'])
      send_email(ses_client, EMAIL_FROM_ADDRESS, EMAIL_TO_ADDRESSES, subject, html)
      #slack
      attachment = [{
          "fallback": "https://aws.amazon.com/blogs/security",
          "pretext": subject,
          "title": trans_title,
          "title_link": url,
          "text": '\n'.join(trans_body_texts),
          "fields": [
              {"title": "Date", "value": published_time, "short": "true"},
              {"title": "Section", "value": section, "short": "true"},
              {"title": "tags", "value": tag, "short": "false"},
          ],
          "mrkdwn_in": ["pretext"],
          "color": "#ed7211"
      }];
      send_message_to_slack(attachment)

    s3_obj_key = '{}/{}-{}.html'.format(S3_OBJ_KEY_PREFIX,
      arrow.get(published_time).format('YYYYMMDD'), doc['doc_id'])
    fwrite_s3(s3_client, html, S3_BUCKET_NAME, s3_obj_key)
    
  LOGGER.debug('done')


if __name__ == '__main__':
  test_sns_event = {
    "Records": [
      {
        "EventSource": "aws:sns",
        "EventVersion": "1.0",
        "EventSubscriptionArn": "arn:aws:sns:us-east-1:{{{accountId}}}:ExampleTopic",
        "Sns": {
          "Type": "Notification",
          "MessageId": "95df01b4-ee98-5cb9-9903-4c221d41eb5e",
          "TopicArn": "arn:aws:sns:us-east-1:123456789012:ExampleTopic",
          "Subject": "example subject",
          "Message": "example message",
          "Timestamp": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
          "SignatureVersion": "1",
          "Signature": "EXAMPLE",
          "SigningCertUrl": "EXAMPLE",
          "UnsubscribeUrl": "EXAMPLE",
          "MessageAttributes": {
            "Test": {
              "Type": "String",
              "Value": "TestString"
            },
            "TestBinary": {
              "Type": "Binary",
              "Value": "TestBinary"
            }
          }
        }
      }
    ]
  }

  msg_body = {
    "id": "6da2a3be3378d3f1",
    "link": "https://aws.amazon.com/blogs/aws/new-redis-6-compatibility-for-amazon-elasticache/",
    "pub_date": "2020-10-07T14:50:59-07:00"
  }
  message = json.dumps(msg_body, ensure_ascii=False)

  test_sns_event['Records'][0]['Sns']['Subject'] = 'blog posts from {topic}'.format(topic='AWS')
  test_sns_event['Records'][0]['Sns']['Message'] = message
  LOGGER.debug(json.dumps(test_sns_event))

  start_t = time.time()
  lambda_handler(test_sns_event, {})
  end_t = time.time()
  LOGGER.info('run_time: {:.2f}'.format(end_t - start_t))
