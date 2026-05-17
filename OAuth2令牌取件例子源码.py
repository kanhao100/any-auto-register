import email
import email.header
import requests
import imaplib

# 1. 使用refresh_token获取access_token
def get_accesstoken(refresh_token):
    data = {
        'client_id': 'dbc8e03a-b00c-46bd-ae65-b683e7707cb0',
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    ret = requests.post('https://login.microsoftonline.com/consumers/oauth2/v2.0/token', data=data)
    print(ret.text)
    return ret.json()['access_token']


def generate_auth_string(user, token):
    auth_string = f"user={user}\1auth=Bearer {token}\1\1"
    return auth_string


def tuple_to_str(tuple_):
    """
    元组转为字符串输出
    :param tuple_: 转换前的元组，QQ邮箱格式为(b'\xcd\xf5\xd4\xc6', 'gbk')或者(b' <XXXX@163.com>', None)，163邮箱格式为('<XXXX@163.com>', None)
    :return: 转换后的字符串
    """
    if tuple_[1]:
        out_str = tuple_[0].decode(tuple_[1])
    else:
        if isinstance(tuple_[0], bytes):
            out_str = tuple_[0].decode('gbk')
        else:
            out_str = tuple_[0]
    return out_str


def fetch_email_body(mail, item):
    try:
        ret, data = mail.fetch(item, '(RFC822)')
        msg = email.message_from_string(data[0][1].decode('utf-8'))
        sub = msg.get('subject')
        sub_text = email.header.decode_header(str(sub))
        # 主题
        sub_detail = ''
        if sub_text[0]:
            sub_detail = tuple_to_str(sub_text[0])
        print(sub_detail)
        for part in msg.walk():
            if not part.is_multipart():
                content_type = part.get_content_type()
                name = part.get_filename()
                if not name:
                    txt = str(part.get_payload(decode=True))
                    if content_type == 'text/html':
                        # mail.store(item, '-FLAGS', '\\SEEN')  设为已读
                        print(txt)
    except Exception as e:
        print(e)


def getmail(sel, mail):
    mail.select(sel)
    status, messages = mail.search(None, 'ALL') #UNSEEN 为未读邮件
    all_emails = messages[0].split()
    print(all_emails)
    for item in all_emails:
        fetch_email_body(mail, item)


# 2. 使用访问令牌连接 IMAP
def connect_imap(emailadr, access_token):
    mail = imaplib.IMAP4_SSL('outlook.live.com')
    print(generate_auth_string(emailadr, access_token))
    mail.authenticate('XOAUTH2', lambda x: generate_auth_string(emailadr, access_token))
    getmail('INBOX', mail) #读取收件箱
    getmail('Junk', mail) #读取垃圾箱
    # 关闭连接
    mail.logout()


# 主流程
if __name__ == '__main__':
    emailadr = 'LeahDavis7849@outlook.com'
    refresh_token = 'M.C528_BL2.0.U.-CnQvP4R1faD1stbCXDizYYQoct4cNOpYvuGuBNYxZGZMquhut2Y853yKTIcsakUBRpZWYbtOVhaRzEOukRY6mJVWeXCVFKf3nNKeRxrjSYoolEPYCGep7XCB59OMrPh7A2a7Q4o00A00aF!!msBEST5FS0cO8kCTsibb0kxwLI2X!hqj7HfRnCsmZgANYRzpdcSdSJ*0GcMZLhYw!fv5qKcGp7FhzpjeiLfQj1xIfbZ9GmET3JfFTDJQClDnphXjZwXsmEv0ENG6vt!f*PLDIfYmsvL5lVOHL5Ptsw*LfKVgXlG6ZnIxLwC0oBGlvS!JueFA2PsRS4nOLf6HhtU3xlQrq4cGhvzcKBeaOsWurxHOVo6IdE9M0wpTbuyz2ElnBK*mpucy5BWz9CwzuYjfa20$'
    access_token = get_accesstoken(refresh_token)
    print("获取的访问令牌:", access_token)
    # 连接 IMAP
    connect_imap(emailadr, access_token)
