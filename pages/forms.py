from django import forms

from .models import ContactMessage


class ContactMessageForm(forms.ModelForm):
    class Meta:
        model = ContactMessage
        fields = ['name', 'email', 'subject', 'message']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Your name'}),
            'email': forms.EmailInput(attrs={'placeholder': 'you@example.com'}),
            'subject': forms.TextInput(attrs={'placeholder': 'How can we help?'}),
            'message': forms.Textarea(
                attrs={'rows': 4, 'placeholder': 'Type your message...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control-app'


class ContactReplyForm(forms.Form):
    reply = forms.CharField(
        max_length=5000,
        widget=forms.Textarea(attrs={
            'class': 'form-control-app',
            'rows': 6,
            'placeholder': 'Write your reply to the patient...',
        }),
    )
